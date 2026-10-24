// Majority checkpoint + LoRA stack, and applying that stack onto another graph.
import { readFileSync } from 'node:fs';

const recipeJs = readFileSync(new URL('../recipe.js', import.meta.url), 'utf8');
eval(recipeJs);
const R = globalThis.ComfyRecipe;
const failures = [];
function check(cond, msg) {
  if (!cond) failures.push(msg);
}

function graph(ckpt, loras, text) {
  const prompt = {
    '1': { class_type: 'CheckpointLoaderSimple', inputs: { ckpt_name: ckpt } },
    '4': { class_type: 'CLIPTextEncode', inputs: { text: text || 'keep this prompt', clip: ['1', 1] } },
    '3': { class_type: 'KSampler', inputs: { seed: 42, steps: 20, cfg: 7, model: ['1', 0], positive: ['4', 0] } },
  };
  loras.forEach((lora, index) => {
    prompt[String(10 + index)] = {
      class_type: 'LoraLoader',
      inputs: {
        lora_name: lora.name,
        strength_model: lora.strength == null ? 1 : lora.strength,
        strength_clip: lora.clip == null ? 1 : lora.clip,
        model: ['1', 0],
        clip: ['1', 1],
      },
    };
  });
  return prompt;
}

const shared = { checkpoint: 'ltx-fav.safetensors', checkpointKey: 'ckpt_name', loras: [
  { name: 'fav-lora.safetensors', strength: 1, clip: 1 },
  { name: 'second-lora.safetensors', strength: 0.8, clip: 1 },
] };
const other = { checkpoint: 'ltx-other.safetensors', checkpointKey: 'ckpt_name', loras: [
  { name: 'other-lora.safetensors', strength: 1, clip: 1 },
] };
const sameNamesDifferentStrength = {
  checkpoint: 'ltx-fav.safetensors',
  checkpointKey: 'ckpt_name',
  loras: [
    { name: 'fav-lora.safetensors', strength: 0.4, clip: 1 },
    { name: 'second-lora.safetensors', strength: 0.8, clip: 1 },
  ],
};
const recipe = R.buildRecipe(
  [shared, other, shared, sameNamesDifferentStrength],
  { scanned: 5, missing: 1, paths: ['a', 'b', 'c', 'd', 'e'] },
);
check(recipe.checkpoint === 'ltx-fav.safetensors', 'checkpoint ' + recipe.checkpoint);
check(recipe.votes === 3, 'votes ' + recipe.votes);
check(recipe.missing === 1, 'missing');
check(recipe.loras.map(l => l.name).join('|') === 'fav-lora.safetensors|second-lora.safetensors', 'lora names');
check(recipe.loras[0].strength === 1, 'strength mode should be 1 not 0.4, got ' + recipe.loras[0].strength);
check(recipe.others.length === 1 && recipe.others[0].checkpoint === 'ltx-other.safetensors', 'other setup');
check(recipe.others[0].count === 1, 'other count');

const tie = R.buildRecipe([
  { checkpoint: 'a.safetensors', checkpointKey: 'ckpt_name', loras: [{ name: 'one.safetensors', strength: 1, clip: null }] },
  { checkpoint: 'b.safetensors', checkpointKey: 'ckpt_name', loras: [{ name: 'one.safetensors', strength: 1, clip: null }, { name: 'two.safetensors', strength: 1, clip: null }] },
]);
check(tie.checkpoint === 'b.safetensors', 'tie prefers the stack with more LoRAs');

const noneStack = R.modelsFromPrompt(graph('ltx-fav.safetensors', [{ name: 'None' }, { name: 'fav-lora.safetensors', strength: 0.5, clip: 0.5 }]));
check(noneStack.checkpoint === 'ltx-fav.safetensors', 'models checkpoint');
check(noneStack.loras.length === 1 && noneStack.loras[0].name === 'fav-lora.safetensors', 'None is not a LoRA');
check(noneStack.loras[0].strength === 0.5 && noneStack.loras[0].clip === 0.5, 'lora strength');

const unet = R.modelsFromPrompt({
  '4': { class_type: 'UNETLoader', inputs: { unet_name: 'ltx-unet.safetensors', weight_dtype: 'fp8' } },
  '9': { class_type: 'LoraLoaderModelOnly', inputs: { lora_name: 'fav-lora.safetensors', strength_model: 1, model: ['4', 0] } },
});
check(unet.checkpoint === 'ltx-unet.safetensors' && unet.checkpointKey === 'unet_name', 'unet checkpoint');

const power = R.modelsFromPrompt({
  '2': {
    class_type: 'Power Lora Loader (rgthree)',
    inputs: {
      model: ['1', 0],
      clip: ['1', 1],
      lora_1: { on: true, lora: 'aaa.safetensors', strength: 0.7, strengthTwo: 0.7 },
      lora_2: { on: false, lora: 'skip.safetensors', strength: 1 },
      lora_10: { on: true, lora: 'zzz.safetensors', strength: 1 },
    },
  },
});
check(power.loras.map(l => l.name).join('|') === 'aaa.safetensors|zzz.safetensors', 'power lora order ' + power.loras.map(l => l.name).join('|'));

const source = graph('clip-ckpt.safetensors', [
  { name: 'clip-lora.safetensors', strength: 0.2, clip: 0.2 },
  { name: 'extra-lora.safetensors', strength: 1, clip: 1 },
  { name: 'another-extra.safetensors', strength: 1, clip: 1 },
], 'a still cat');
const applied = R.applyRecipeToPrompt(source, recipe);
check(applied.prompt['4'].inputs.text === 'a still cat', 'prompt text stays');
check(applied.prompt['3'].inputs.seed === 42, 'seed stays');
check(applied.prompt['1'].inputs.ckpt_name === 'ltx-fav.safetensors', 'ckpt replaced');
check(applied.prompt['10'].inputs.lora_name === 'fav-lora.safetensors', 'first lora');
check(applied.prompt['10'].inputs.strength_model === 1, 'first strength');
check(applied.prompt['11'].inputs.lora_name === 'second-lora.safetensors', 'second lora');
check(applied.prompt['11'].inputs.strength_model === 0.8, 'second strength');
check(applied.prompt['12'].inputs.strength_model === 0, 'extra lora off');
check(applied.prompt['12'].inputs.lora_name === 'fav-lora.safetensors', 'extra lora file is from the recipe');
check(applied.disabled === 1, 'disabled ' + applied.disabled);
check(applied.prompt['10'].inputs.model[0] === '1', 'links stay');

const short = R.applyRecipeToPrompt(graph('clip-ckpt.safetensors', [{ name: 'only.safetensors' }]), recipe);
check(short.missingSlots === 1, 'missing slot ' + short.missingSlots);
check(short.prompt['4'].inputs.text === 'keep this prompt', 'short graph keeps prompt');

const unetApplied = R.applyRecipeToPrompt({
  '4': { class_type: 'UNETLoader', inputs: { unet_name: 'old-unet.safetensors' } },
  '6': { class_type: 'CLIPTextEncode', inputs: { text: 'video line' } },
}, { checkpoint: 'ltx-fav.safetensors', checkpointKey: 'ckpt_name', loras: [] });
check(unetApplied.prompt['4'].inputs.unet_name === 'ltx-fav.safetensors', 'falls back to unet loader');
check(unetApplied.prompt['6'].inputs.text === 'video line', 'unet graph keeps text');

const restored = R.restoreModels(applied.prompt, source);
check(restored['1'].inputs.ckpt_name === 'clip-ckpt.safetensors', 'restore ckpt');
check(restored['10'].inputs.lora_name === 'clip-lora.safetensors', 'restore lora');
check(restored['4'].inputs.text === 'a still cat', 'restore keeps prompt text');
check(restored['3'].inputs.seed === 42, 'restore keeps seed');

const memory = new Map();
const storage = {
  getItem: (key) => (memory.has(key) ? memory.get(key) : null),
  setItem: (key, value) => memory.set(key, String(value)),
};
R.writeRecipe(storage, recipe);
check(R.readRecipe(storage).checkpoint === 'ltx-fav.safetensors', 'stored');
R.clearRecipe(storage);
check(R.readRecipe(storage) == null && R.isCleared(storage), 'cleared');

if (failures.length) {
  console.error(failures.join('\n'));
  process.exit(1);
}
console.log('recipe ok');
