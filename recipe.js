/* Favorite-video recipe: the checkpoint and LoRAs most favorites share. */
(function (root) {
    'use strict';

    var RECIPE_KEY = 'comfyVideoRecipe';

    function numOrNull(value) {
        if (typeof value === 'number' && !isNaN(value)) return value;
        if (typeof value === 'string' && value.trim() && !isNaN(Number(value))) return Number(value);
        return null;
    }

    function usableLoraName(name) {
        var text = String(name || '').trim();
        if (!text || /^none$/i.test(text)) return '';
        return text;
    }

    function nodeBlob(node) {
        var ct = node && node.class_type ? String(node.class_type) : '';
        var title = node && node._meta && node._meta.title ? String(node._meta.title) : '';
        return ct + ' ' + title;
    }

    function sortedIds(prompt) {
        return Object.keys(prompt).sort(function (a, b) {
            return a.localeCompare(b, undefined, { numeric: true });
        });
    }

    function eachNode(prompt, fn) {
        if (!prompt || typeof prompt !== 'object' || Array.isArray(prompt)) return;
        sortedIds(prompt).forEach(function (id) {
            var node = prompt[id];
            if (!node || typeof node !== 'object' || !node.inputs || typeof node.inputs !== 'object') return;
            fn(String(id), node, node.inputs);
        });
    }

    function collectCheckpointSlots(prompt, includeEmpty) {
        var slots = [];
        eachNode(prompt, function (id, node, inputs) {
            if (typeof inputs.ckpt_name === 'string' && (inputs.ckpt_name.trim() || includeEmpty)) {
                slots.push({ id: id, key: 'ckpt_name', name: inputs.ckpt_name.trim() });
            }
            if (typeof inputs.unet_name === 'string' && (inputs.unet_name.trim() || includeEmpty)) {
                slots.push({ id: id, key: 'unet_name', name: inputs.unet_name.trim() });
            }
        });
        return slots;
    }

    function collectLoraSlots(prompt, includeEmpty) {
        var slots = [];
        eachNode(prompt, function (id, node, inputs) {
            var blob = nodeBlob(node);
            if (typeof inputs.lora_name === 'string') {
                var name = usableLoraName(inputs.lora_name);
                if (name || includeEmpty) {
                    slots.push({
                        id: id,
                        key: 'lora_name',
                        kind: 'string',
                        name: name,
                        strength: numOrNull(inputs.strength_model != null ? inputs.strength_model : inputs.strength),
                        clip: numOrNull(inputs.strength_clip),
                    });
                }
            }
            Object.keys(inputs).filter(function (key) { return key !== 'lora_name'; }).sort(function (a, b) {
                return a.localeCompare(b, undefined, { numeric: true });
            }).forEach(function (key) {
                var value = inputs[key];
                if (!value || typeof value !== 'object' || Array.isArray(value)) return;
                if (typeof value.lora !== 'string' && typeof value.lora_name !== 'string') return;
                if (!/lora/i.test(blob + ' ' + key)) return;
                var objName = usableLoraName(typeof value.lora === 'string' ? value.lora : value.lora_name);
                if (value.on === false && !includeEmpty) return;
                if (!objName && !includeEmpty) return;
                slots.push({
                    id: id,
                    key: key,
                    kind: 'object',
                    name: objName,
                    strength: numOrNull(value.strength != null ? value.strength : value.strength_model),
                    clip: numOrNull(value.strengthTwo != null ? value.strengthTwo : value.strength_clip),
                });
            });
        });
        return slots;
    }

    function modelsFromPrompt(prompt) {
        var checkpoints = collectCheckpointSlots(prompt);
        var chosen = null;
        checkpoints.forEach(function (slot) {
            if (!chosen && slot.key === 'ckpt_name') chosen = slot;
        });
        if (!chosen) {
            checkpoints.forEach(function (slot) {
                if (!chosen && slot.key === 'unet_name') chosen = slot;
            });
        }
        return {
            checkpoint: chosen ? chosen.name : '',
            checkpointKey: chosen ? chosen.key : '',
            loras: collectLoraSlots(prompt, false).map(function (slot) {
                return { name: slot.name, strength: slot.strength, clip: slot.clip };
            }),
        };
    }

    function modeNumber(values) {
        var counts = {};
        var bestKey = null;
        var bestCount = -1;
        values.forEach(function (value) {
            if (value == null || value === '' || isNaN(Number(value))) return;
            var key = String(Number(value));
            counts[key] = (counts[key] || 0) + 1;
            if (counts[key] > bestCount) {
                bestCount = counts[key];
                bestKey = key;
            }
        });
        return bestKey == null ? null : Number(bestKey);
    }

    function modeText(values) {
        var counts = {};
        var best = '';
        var bestCount = -1;
        values.forEach(function (value) {
            var key = value == null ? '' : String(value);
            if (!key) return;
            counts[key] = (counts[key] || 0) + 1;
            if (counts[key] > bestCount) {
                bestCount = counts[key];
                best = key;
            }
        });
        return best;
    }

    function stackKey(stack) {
        return stack.checkpoint + '\n' + stack.loras.map(function (lora) { return lora.name; }).join('\n');
    }

    function cleanLora(name, strength, clip) {
        var out = { name: name };
        if (typeof strength === 'number' && !isNaN(strength)) out.strength = strength;
        if (typeof clip === 'number' && !isNaN(clip)) out.clip = clip;
        return out;
    }

    function buildRecipe(stacks, info) {
        var groups = [];
        var byKey = {};
        (stacks || []).forEach(function (stack) {
            if (!stack || !stack.checkpoint) return;
            var key = stackKey(stack);
            if (!byKey[key]) {
                byKey[key] = { key: key, list: [] };
                groups.push(byKey[key]);
            }
            byKey[key].list.push(stack);
        });
        if (!groups.length) return null;
        var best = groups[0];
        groups.forEach(function (group) {
            var richer = group.list.length === best.list.length
                && (group.list[0].loras || []).length > (best.list[0].loras || []).length;
            if (group.list.length > best.list.length || richer) best = group;
        });
        var sample = best.list[0];
        var loras = (sample.loras || []).map(function (lora, index) {
            return cleanLora(
                lora.name,
                modeNumber(best.list.map(function (stack) { return stack.loras[index] && stack.loras[index].strength; })),
                modeNumber(best.list.map(function (stack) { return stack.loras[index] && stack.loras[index].clip; }))
            );
        });
        var meta = info || {};
        var others = groups.filter(function (group) { return group !== best; }).map(function (group) {
            var first = group.list[0];
            return {
                checkpoint: first.checkpoint,
                loras: (first.loras || []).map(function (lora) { return lora.name; }),
                count: group.list.length,
            };
        }).sort(function (a, b) { return b.count - a.count; });
        return {
            checkpoint: sample.checkpoint,
            checkpointKey: modeText(best.list.map(function (stack) { return stack.checkpointKey; })) || sample.checkpointKey || 'ckpt_name',
            loras: loras,
            votes: best.list.length,
            scanned: meta.scanned != null ? meta.scanned : best.list.length,
            missing: meta.missing || 0,
            others: others,
            paths: meta.paths || [],
        };
    }

    function assignLoraSlot(inputs, slot, lora) {
        if (slot.kind === 'object') {
            var obj = Object.assign({}, inputs[slot.key]);
            if (typeof obj.lora === 'string' || !('lora_name' in obj)) obj.lora = lora.name;
            else obj.lora_name = lora.name;
            obj.on = true;
            if (lora.strength != null) {
                if ('strength' in obj) obj.strength = lora.strength;
                else if ('strength_model' in obj) obj.strength_model = lora.strength;
                else obj.strength = lora.strength;
            }
            if (lora.clip != null) {
                if ('strengthTwo' in obj) obj.strengthTwo = lora.clip;
                else if ('strength_clip' in obj) obj.strength_clip = lora.clip;
            }
            inputs[slot.key] = obj;
            return;
        }
        inputs.lora_name = lora.name;
        if (lora.strength != null) {
            if ('strength_model' in inputs) inputs.strength_model = lora.strength;
            else if ('strength' in inputs) inputs.strength = lora.strength;
        }
        if (lora.clip != null && 'strength_clip' in inputs) inputs.strength_clip = lora.clip;
    }

    function disableLoraSlot(inputs, slot, fallbackName) {
        if (slot.kind === 'object') {
            var obj = Object.assign({}, inputs[slot.key]);
            obj.on = false;
            inputs[slot.key] = obj;
            return true;
        }
        var turnedOff = false;
        if ('strength_model' in inputs) { inputs.strength_model = 0; turnedOff = true; }
        if ('strength' in inputs) { inputs.strength = 0; turnedOff = true; }
        if ('strength_clip' in inputs) inputs.strength_clip = 0;
        if (turnedOff && fallbackName) inputs.lora_name = fallbackName;
        return turnedOff;
    }

    function applyRecipeToPrompt(prompt, recipe) {
        var next = JSON.parse(JSON.stringify(prompt || {}));
        var report = {
            prompt: next,
            checkpointApplied: false,
            checkpoints: 0,
            lorasApplied: 0,
            disabled: 0,
            missingSlots: 0,
            uncontrolled: 0,
        };
        if (!recipe || !recipe.checkpoint) return report;
        var checkpoints = collectCheckpointSlots(next, true);
        var wanted = recipe.checkpointKey || 'ckpt_name';
        var targets = checkpoints.filter(function (slot) { return slot.key === wanted; });
        if (!targets.length) targets = checkpoints.filter(function (slot) { return slot.key !== wanted; });
        targets.forEach(function (slot) {
            next[slot.id].inputs[slot.key] = recipe.checkpoint;
        });
        report.checkpoints = targets.length;
        report.checkpointApplied = targets.length > 0;
        var slots = collectLoraSlots(next, true);
        var loras = recipe.loras || [];
        var fallback = loras[0] && loras[0].name;
        slots.forEach(function (slot, index) {
            var inputs = next[slot.id].inputs;
            if (index < loras.length) {
                assignLoraSlot(inputs, slot, loras[index]);
                report.lorasApplied += 1;
            } else if (disableLoraSlot(inputs, slot, fallback)) {
                report.disabled += 1;
            } else {
                report.uncontrolled += 1;
            }
        });
        report.missingSlots = Math.max(0, loras.length - slots.length);
        return report;
    }

    function isLoraNode(node) {
        return /lora/i.test(nodeBlob(node || {}));
    }

    function isModelInput(key, value, node) {
        if (key === 'ckpt_name' || key === 'unet_name' || key === 'lora_name' || key === 'strength_model' || key === 'strength_clip') return true;
        if (key === 'strength' && isLoraNode(node)) return true;
        return !!(value && typeof value === 'object' && !Array.isArray(value)
            && (typeof value.lora === 'string' || typeof value.lora_name === 'string'));
    }

    function restoreModels(prompt, base) {
        var next = JSON.parse(JSON.stringify(prompt || {}));
        var src = base || {};
        Object.keys(next).forEach(function (id) {
            var node = next[id];
            var from = src[id];
            if (!node || !from || !node.inputs || !from.inputs) return;
            Object.keys(node.inputs).forEach(function (key) {
                if (!(key in from.inputs)) return;
                if (!isModelInput(key, from.inputs[key], from) && !isModelInput(key, node.inputs[key], node)) return;
                node.inputs[key] = JSON.parse(JSON.stringify(from.inputs[key]));
            });
        });
        return next;
    }

    function readRaw(storage) {
        try {
            var raw = JSON.parse(storage.getItem(RECIPE_KEY) || 'null');
            if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
            return raw;
        } catch (err) {
            return null;
        }
    }

    function readRecipe(storage) {
        var raw = readRaw(storage);
        if (!raw || raw.cleared || typeof raw.checkpoint !== 'string' || !raw.checkpoint) return null;
        if (!Array.isArray(raw.loras)) raw.loras = [];
        return raw;
    }

    function writeRecipe(storage, recipe) {
        var prev = readRaw(storage);
        var next = Object.assign({}, recipe || {});
        if (next.enabled == null) next.enabled = prev && typeof prev.enabled === 'boolean' ? prev.enabled : true;
        next.cleared = false;
        if (!Array.isArray(next.loras)) next.loras = [];
        storage.setItem(RECIPE_KEY, JSON.stringify(next));
        return next;
    }

    function clearRecipe(storage) {
        storage.setItem(RECIPE_KEY, JSON.stringify({
            cleared: true,
            enabled: false,
            checkpoint: '',
            loras: [],
        }));
    }

    function isCleared(storage) {
        var raw = readRaw(storage);
        return !!(raw && raw.cleared);
    }

    root.ComfyRecipe = {
        RECIPE_KEY: RECIPE_KEY,
        modelsFromPrompt: modelsFromPrompt,
        buildRecipe: buildRecipe,
        applyRecipeToPrompt: applyRecipeToPrompt,
        restoreModels: restoreModels,
        readRecipe: readRecipe,
        writeRecipe: writeRecipe,
        clearRecipe: clearRecipe,
        isCleared: isCleared,
    };
})(typeof globalThis !== 'undefined' ? globalThis : this);
