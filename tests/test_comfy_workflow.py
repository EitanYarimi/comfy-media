#!/usr/bin/env python3
"""ComfyUI API prompt extraction and form fields for Remix."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import video_server  # noqa: E402

SAMPLE_PROMPT = {
    '1': {
        'class_type': 'CheckpointLoaderSimple',
        'inputs': {'ckpt_name': 'model.safetensors'},
    },
    '4': {
        'class_type': 'CLIPTextEncode',
        '_meta': {'title': 'Positive prompt'},
        'inputs': {'text': 'a cat', 'clip': ['1', 1]},
    },
    '5': {
        'class_type': 'CLIPTextEncode',
        'inputs': {'text': 'blurry', 'clip': ['1', 1]},
    },
    '6': {
        'class_type': 'EmptyLatentImage',
        'inputs': {'width': 640, 'height': 480, 'batch_size': 1},
    },
    '3': {
        'class_type': 'KSampler',
        'inputs': {
            'seed': 42,
            'steps': 20,
            'cfg': 7,
            'sampler_name': 'euler',
            'scheduler': 'normal',
            'denoise': 1,
            'model': ['1', 0],
            'positive': ['4', 0],
            'negative': ['5', 0],
            'latent_image': ['6', 0],
        },
    },
    '8': {
        'class_type': 'VAEDecode',
        'inputs': {'samples': ['3', 0], 'vae': ['1', 2]},
    },
}


class CoercePromptTests(unittest.TestCase):
    def test_api_dict(self):
        self.assertEqual(video_server.coerce_comfy_prompt(SAMPLE_PROMPT)['4']['class_type'], 'CLIPTextEncode')

    def test_nested_prompt_key(self):
        wrapped = {'prompt': SAMPLE_PROMPT, 'extra': 1}
        self.assertIsNotNone(video_server.coerce_comfy_prompt(wrapped))

    def test_json_string(self):
        self.assertIsNotNone(video_server.coerce_comfy_prompt(json.dumps(SAMPLE_PROMPT)))

    def test_rejects_ui_workflow(self):
        self.assertIsNone(video_server.coerce_comfy_prompt({'nodes': [{'id': 1}]}))


class PromptToFieldsTests(unittest.TestCase):
    def setUp(self):
        self.fields = video_server.prompt_to_fields(SAMPLE_PROMPT)

    def test_prompts_come_first(self):
        self.assertEqual(self.fields[0]['section'], 'prompts')
        self.assertEqual(self.fields[0]['role'], 'positive')
        self.assertEqual(self.fields[0]['label'], 'Positive prompt')
        self.assertEqual(self.fields[0]['value'], 'a cat')
        neg = next(f for f in self.fields if f['role'] == 'negative')
        self.assertEqual(neg['value'], 'blurry')
        self.assertLess(
            [f['role'] for f in self.fields].index('positive'),
            [f['role'] for f in self.fields].index('negative'),
        )

    def test_sampler_widgets_are_not_links(self):
        keys = {(f['node_id'], f['key']) for f in self.fields}
        self.assertIn(('3', 'seed'), keys)
        self.assertIn(('3', 'steps'), keys)
        self.assertNotIn(('3', 'positive'), keys)
        self.assertNotIn(('3', 'model'), keys)

    def test_latent_size_is_editable(self):
        width = next(f for f in self.fields if f['key'] == 'width')
        self.assertEqual(width['section'], 'widgets')
        self.assertEqual(width['type'], 'number')

    def test_vae_is_advanced(self):
        self.assertTrue(all(f['section'] == 'advanced' for f in self.fields if f['class_type'] == 'VAEDecode'))

    def test_all_clip_encodes_are_prompts(self):
        prompt = dict(SAMPLE_PROMPT)
        prompt['9'] = {
            'class_type': 'CLIPTextEncode',
            '_meta': {'title': 'CLIP Text Encode (Prompt)'},
            'inputs': {'text': 'second mix', 'clip': ['1', 1]},
        }
        prompt['10'] = {
            'class_type': 'WanVideoTextEncode',
            'inputs': {'positive_prompt': 'video pos', 'negative_prompt': 'video neg'},
        }
        fields = video_server.prompt_to_fields(prompt)
        prompt_fields = [f for f in fields if f['section'] == 'prompts']
        self.assertGreaterEqual(len(prompt_fields), 4)
        self.assertTrue(all(f['type'] == 'textarea' for f in prompt_fields))
        extra = next(f for f in prompt_fields if f['node_id'] == '9')
        self.assertEqual(extra['role'], 'prompt')
        self.assertIn('prompt', extra['label'].lower())
        self.assertNotIn('CLIP Text Encode', extra['label'])
        wan_pos = next(f for f in prompt_fields if f['key'] == 'positive_prompt')
        self.assertEqual(wan_pos['role'], 'positive')
        wan_neg = next(f for f in prompt_fields if f['key'] == 'negative_prompt')
        self.assertEqual(wan_neg['role'], 'negative')
        self.assertNotIn(
            ('10', 'positive_prompt'),
            {(f['node_id'], f['key']) for f in fields if f['section'] == 'widgets'},
        )

    def test_value_key_is_video_positive(self):
        prompt = {
            '4': {
                'class_type': 'CLIPTextEncode',
                '_meta': {'title': 'Prompt'},
                'inputs': {'value': 'this is the video positive', 'clip': ['1', 1]},
            },
            '5': {
                'class_type': 'CLIPTextEncode',
                '_meta': {'title': 'Prompt'},
                'inputs': {'value': 'video negative', 'clip': ['1', 1]},
            },
            '3': {
                'class_type': 'KSampler',
                'inputs': {
                    'seed': 1,
                    'steps': 4,
                    'cfg': 1,
                    'sampler_name': 'euler',
                    'scheduler': 'normal',
                    'denoise': 1,
                    'model': ['1', 0],
                    'positive': ['4', 0],
                    'negative': ['5', 0],
                    'latent_image': ['6', 0],
                },
            },
        }
        fields = video_server.prompt_to_fields(prompt)
        pos = next(f for f in fields if f['node_id'] == '4')
        self.assertEqual(pos['section'], 'prompts')
        self.assertEqual(pos['role'], 'positive')
        self.assertEqual(pos['key'], 'value')
        self.assertEqual(pos['label'], 'Video positive')
        self.assertNotIn('value', pos['label'].lower())
        self.assertNotEqual(pos['label'], 'Prompt · value')
        neg = next(f for f in fields if f['node_id'] == '5')
        self.assertEqual(neg['section'], 'prompts')
        self.assertEqual(neg['role'], 'negative')
        self.assertEqual(neg['label'], 'Video negative')

    def test_clip_text_is_image_prompt_when_video_exists(self):
        prompt = {
            '6': {
                'class_type': 'CLIPTextEncode',
                '_meta': {'title': 'CLIP Text Encode (Prompt)'},
                'inputs': {'text': 'score_9, 1girl, detailed face', 'clip': ['1', 1]},
            },
            '7': {
                'class_type': 'CLIPTextEncode',
                '_meta': {'title': 'Prompt'},
                'inputs': {'value': 'this is the video positive', 'clip': ['1', 1]},
            },
            '8': {
                'class_type': 'CLIPTextEncode',
                '_meta': {'title': 'Prompt'},
                'inputs': {'value': 'video negative', 'clip': ['1', 1]},
            },
            '3': {
                'class_type': 'KSampler',
                'inputs': {
                    'seed': 1,
                    'steps': 4,
                    'cfg': 1,
                    'sampler_name': 'euler',
                    'scheduler': 'normal',
                    'denoise': 1,
                    'model': ['1', 0],
                    'positive': ['7', 0],
                    'negative': ['8', 0],
                    'latent_image': ['9', 0],
                },
            },
        }
        fields = video_server.prompt_to_fields(prompt)
        prompt_fields = [f for f in fields if f['section'] == 'prompts']
        labels = [f['label'] for f in prompt_fields]
        self.assertIn('Image prompt', labels)
        self.assertIn('Video positive', labels)
        self.assertIn('Video negative', labels)
        image = next(f for f in prompt_fields if f['node_id'] == '6')
        self.assertEqual(image['key'], 'text')
        self.assertEqual(image['section'], 'prompts')
        self.assertIn('1girl', image['value'])
        self.assertNotIn('CLIP Text Encode', image['label'])
        pos = next(f for f in prompt_fields if f['node_id'] == '7')
        self.assertEqual(pos['role'], 'positive')
        self.assertEqual(pos['label'], 'Video positive')

    def test_clip_encode_prompt_title_not_in_prompts(self):
        prompt = {
            '6': {
                'class_type': 'CLIPTextEncode',
                '_meta': {'title': 'CLIP Text Encode (Prompt)'},
                'inputs': {'value': '1011:266,0', 'clip': ['1', 1]},
            },
            '7': {
                'class_type': 'CLIPTextEncode',
                '_meta': {'title': 'Prompt'},
                'inputs': {'value': 'this is the video positive', 'clip': ['1', 1]},
            },
            '3': {
                'class_type': 'KSampler',
                'inputs': {
                    'seed': 1, 'steps': 1, 'cfg': 1, 'sampler_name': 'euler',
                    'scheduler': 'normal', 'denoise': 1, 'model': ['1', 0],
                    'positive': ['7', 0], 'negative': ['6', 0], 'latent_image': ['9', 0],
                },
            },
        }
        fields = video_server.prompt_to_fields(prompt)
        prompt_fields = [f for f in fields if f['section'] == 'prompts']
        self.assertFalse(any('CLIP Text Encode' in str(f.get('label') or '') for f in prompt_fields))
        self.assertFalse(any(str(f.get('value') or '') == '1011:266,0' and f['section'] == 'prompts' for f in fields))
        pos = next(f for f in prompt_fields if f['key'] == 'value')
        self.assertEqual(pos['role'], 'positive')
        self.assertEqual(pos['label'], 'Video positive')
        self.assertIn('video positive', str(pos['value']))

    def test_ltx_primitive_string_is_video_prompt(self):
        prompt = {
            '1011:266': {
                'class_type': 'PrimitiveStringMultiline',
                '_meta': {'title': 'Prompt'},
                'inputs': {'value': 'Ultra photo-realistic 15-second video from the still'},
            },
            '1011:240': {
                'class_type': 'CLIPTextEncode',
                '_meta': {'title': 'CLIP Text Encode (Prompt)'},
                'inputs': {'text': '1011:266,0', 'clip': ['1011:243', 0]},
            },
            '3': {
                'class_type': 'CLIPTextEncode',
                '_meta': {'title': 'CLIP Text Encode (Prompt)'},
                'inputs': {'text': 'score_9, 1girl, detailed face', 'clip': ['2', 0]},
            },
            '1011:257': {
                'class_type': 'PrimitiveInt',
                '_meta': {'title': 'Width'},
                'inputs': {'value': 480},
            },
            '5': {
                'class_type': 'KSampler',
                'inputs': {
                    'seed': 1, 'steps': 1, 'cfg': 1, 'sampler_name': 'euler',
                    'scheduler': 'normal', 'denoise': 1, 'model': ['1', 0],
                    'positive': ['3', 0], 'negative': ['1011:240', 0],
                    'latent_image': ['9', 0],
                },
            },
        }
        fields = video_server.prompt_to_fields(prompt)
        prompt_fields = [f for f in fields if f['section'] == 'prompts']
        self.assertEqual(len(prompt_fields), 2)
        image = next(f for f in prompt_fields if f['node_id'] == '3')
        self.assertEqual(image['key'], 'text')
        self.assertEqual(image['label'], 'Image positive')
        self.assertIn('1girl', image['value'])
        video = next(f for f in prompt_fields if f['class_type'] == 'PrimitiveStringMultiline')
        self.assertEqual(video['key'], 'value')
        self.assertEqual(video['role'], 'positive')
        self.assertEqual(video['label'], 'Video positive')
        self.assertIn('Ultra photo-realistic', video['value'])
        self.assertFalse(any('CLIP Text Encode' in str(f.get('label') or '') for f in prompt_fields))
        self.assertFalse(any(str(f.get('value') or '') == '1011:266,0' and f['section'] == 'prompts' for f in fields))
        width = next(f for f in fields if f['node_id'] == '1011:257')
        self.assertNotEqual(width['section'], 'prompts')

    def test_empty_prompt(self):
        self.assertEqual(video_server.prompt_to_fields(None), [])
        self.assertEqual(video_server.prompt_to_fields({}), [])


class ExtractWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sidecar_json_next_to_mp4(self):
        clip = self.root / 'clip.mp4'
        clip.write_bytes(b'not-a-real-video')
        (self.root / 'clip.mp4.json').write_text(json.dumps(SAMPLE_PROMPT), encoding='utf-8')
        payload = video_server.extract_comfy_workflow(clip)
        self.assertIsNotNone(payload['prompt'])
        self.assertTrue(payload['source'].startswith('sidecar:'))
        self.assertGreater(len(payload['fields']), 0)
        self.assertNotIn('error', payload)

    def test_stem_json_sidecar(self):
        clip = self.root / 'shot.webm'
        clip.write_bytes(b'x')
        (self.root / 'shot.json').write_text(json.dumps({'prompt': SAMPLE_PROMPT}), encoding='utf-8')
        payload = video_server.extract_comfy_workflow(clip)
        self.assertEqual(payload['prompt']['4']['inputs']['text'], 'a cat')

    def test_missing_metadata_is_empty_not_crash(self):
        clip = self.root / 'empty.mp4'
        clip.write_bytes(b'x')
        payload = video_server.extract_comfy_workflow(clip)
        self.assertIsNone(payload['prompt'])
        self.assertEqual(payload['fields'], [])
        self.assertIn('error', payload)

    def test_png_text_chunks(self):
        try:
            from PIL import Image
            from PIL.PngImagePlugin import PngInfo
        except ImportError:
            self.skipTest('Pillow not installed')
        img_path = self.root / 'out.png'
        info = PngInfo()
        info.add_text('prompt', json.dumps(SAMPLE_PROMPT))
        Image.new('RGB', (8, 8), (10, 20, 30)).save(img_path, pnginfo=info)
        payload = video_server.extract_comfy_workflow(img_path)
        self.assertEqual(payload['source'], 'image:prompt')
        self.assertEqual(payload['fields'][0]['value'], 'a cat')


class FrontendRemixTests(unittest.TestCase):
    def test_run_page_exists(self):
        src = (Path(ROOT) / 'run.html').read_text()
        self.assertIn("localStorage.getItem(COLAB_KEY)", src)
        self.assertIn('/prompt', src)
        self.assertIn('/history/', src)
        self.assertIn('/system_stats', src)
        self.assertIn('/ws', src)
        self.assertIn('id="runLog"', src)
        self.assertIn('function persistRemix', src)
        self.assertIn('comfyRemixState', src)
        self.assertIn('function resumeRun', src)
        self.assertIn('function renderField', src)
        self.assertIn('prompts.forEach(f => { html += renderField(f, true); })', src)
        self.assertIn('prompts-section', src)
        self.assertIn('prompt-field', src)
        self.assertIn('function buildFieldsClient', src)
        self.assertIn('function isPromptInputKey', src)
        self.assertIn('Image positive', src)
        self.assertIn('function graphHasVideoPrompt', src)
        self.assertIn('treatValueAsPrompt', src)
        self.assertIn('function demoteStockClipTextFields', src)
        self.assertIn('function isJunkPromptValue', src)
        self.assertIn('function parseSerializedLink', src)
        self.assertIn('PrimitiveString', src)
        self.assertIn('workflow.fields = buildFieldsClient(workflow.prompt)', src)
        self.assertIn('CLIP Text Encode', src)
        self.assertIn('rows="${rows}"', src)
        self.assertNotIn('id="outputPreview"', src)
        self.assertNotIn('outputs-preview', src)
        self.assertNotIn('live-preview-img', src)
        self.assertIn('id="outputs"', src)
        self.assertIn('output-slot still', src)
        self.assertIn('id="runTabs"', src)
        self.assertIn('function beginRunTab', src)
        self.assertIn('function selectTab', src)
        self.assertIn('function appendLogDom', src)
        self.assertIn('escapeHtml(text)', src)
        self.assertIn('function isVideoFile', src)
        self.assertIn('Encoding video…', src)
        self.assertIn('output-pair', src)
        self.assertIn('function renderOutputPairs', src)
        self.assertIn('function handleWsBinary', src)
        self.assertIn('function filesFromExecuted', src)
        self.assertIn('function normalizeOutputFile', src)
        self.assertIn('retryOutputImg', src)
        self.assertIn('media-frame', src)
        self.assertIn('function showLivePreview', src)
        self.assertIn('binaryType', src)
        self.assertIn('id="runCount"', src)
        self.assertIn('playsinline muted', src)
        self.assertIn('run ${i} of ${runTotal}', src)
        self.assertIn('id="mediaLightbox"', src)
        self.assertIn('function openMediaLightbox', src)
        self.assertIn('playsinline muted', src)
        self.assertIn('v.muted = true', src)
        self.assertIn('id="stopBtn"', src)
        self.assertIn('function stopRun', src)
        self.assertIn('id="cancelBtn"', src)
        self.assertIn('function enqueueStandby', src)
        self.assertIn('function cancelStandby', src)
        self.assertIn('function drainStandby', src)
        self.assertIn('id="standbyList"', src)
        self.assertIn('runBtn.disabled = false', src)
        self.assertIn('/interrupt', src)
        self.assertIn('JSON.stringify({ clear: true })', src)
        self.assertIn('Stopped', src)
        self.assertIn('id="randomSeed"', src)
        self.assertIn('function applyRandomSeeds', src)
        self.assertIn('function randomSeedValue', src)
        self.assertIn('0x100000000', src)
        self.assertIn('function colabFetch', src)
        self.assertIn('function isNetworkFetchError', src)
        self.assertIn('Colab unreachable, retrying history', src)
        self.assertIn('websocket dropped · reconnecting', src)
        self.assertIn('/api/workflow?path=', src)

    def test_gallery_remix_links(self):
        src = (Path(ROOT) / 'index.html').read_text()
        self.assertIn('function openRemixFromModal', src)
        self.assertIn('run.html?path=', src)
        self.assertIn('id="remixNav"', src)
        self.assertIn('comfyRemixPath', src)

    def test_server_serves_run_html(self):
        src = (Path(ROOT) / 'video_server.py').read_text()
        self.assertIn("'/run.html'", src)
        self.assertIn('/api/workflow', src)
        self.assertIn('APP_HTML_PAGES', src)
        self.assertIn('def serve_app_html', src)


if __name__ == '__main__':
    unittest.main()
