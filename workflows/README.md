# Same-room couple — three scenes

Import [`same-room-couple.json`](same-room-couple.json) in ComfyUI (**Load** / drag onto the canvas). It is a fork of your Pony txt2img graph (`cyberrealisticPony_v127Alt` + **Pony Realism Slider** 1.8, 1216×832, `dpmpp_2m` / karras, 20 steps, CFG 2.5).

Identity uses the **same InstantID stack** as `linkedin_instantid_00427_.json`: `ip-adapter.bin`, `instantid_controlnet.safetensors`, Face Analysis **CUDA**, **ApplyInstantIDAdvanced** at `ip 0.8 / cn 0.8 / start 0.3 / end 1 / noise 0.2 / concat`. `image_kps` is left unconnected so sofa and POV poses can change (keypoints from the hug would freeze that camera).

One queue writes three stills of the **same two adults in the same dark bar**:

| File prefix | Scene | How it is sampled |
|---|---|---|
| `scene1_hug_bar` | Hugging at the bar counter | txt2img, denoise **1.0** (master) |
| `scene2_sofa` | Sitting on the lounge sofa in that bar | img2img + InstantID + Depth **0.65**, denoise **0.50** |
| `scene3_kneeling_pov` | He looks at her; she kneeling; his POV | img2img + InstantID, **no Depth**, denoise **0.60** — POV from the checkpoint prompt |

The depth preprocessor is **only for scene 2**. InstantID `image` defaults to the scene 1 still.

## Custom nodes and models

Install with ComfyUI Manager:

- [ComfyUI_InstantID](https://github.com/cubiq/ComfyUI_InstantID) (same pack as the LinkedIn workflow)
- [comfyui_controlnet_aux](https://github.com/Fannovel16/comfyui_controlnet_aux) (sofa Depth only)

| Node | File / setting |
|---|---|
| Checkpoint | `Copy of Copy of Copy of cyberrealisticPony_v127Alt.safetensors` |
| LoRA | `Pony Realism Slider.safetensors` |
| InstantID Model | `models/instantid/ip-adapter.bin` |
| InstantID ControlNet | `instantid_controlnet.safetensors` |
| InsightFace | `models/insightface/models/antelopev2/*.onnx` |
| Face Analysis | **CUDA** (same as your LinkedIn graph; switch to CPU/ROCM if that is what you run) |
| SDXL Depth ControlNet | sofa scene only, e.g. `diffusers_xl_depth_full.safetensors` |

InstantID is built for **one primary face**. On a two-person still it usually locks the largest/closest face. For a dedicated identity photo, load it in **OPTIONAL FACE REFERENCE** and reconnect that `IMAGE` into both ApplyInstantID `image` inputs.

## Queue order

1. **Queue Prompt.** Scene 1 generates the master hug; InstantID + scenes 2/3 run from that image.
2. When the couple and bar look right, set scene 1’s seed to **fixed**.
3. To reuse a PNG: load it, reconnect `IMAGE` to both ApplyInstantID `image` inputs, Depth preprocessor, and VAEEncode. Mute scene 1’s KSampler if you do not want a new hug.

Do **not** edit **LOOK**, **ROOM**, or **PEOPLE**. Only the three **ACTION** boxes change pose/camera.

## Frozen vs action prompts

**ROOM (frozen):** dark bar at night, counter, bottles, dim practicals, lounge sofa on the wall in the *same* bar.

**PEOPLE (frozen):** same young woman (club dress) and same older man (suit).

**ACTION 1 — hug:** both standing at the bar, embracing, side/three-quarter, full body.

**ACTION 2 — sofa:** both sitting on that bar’s lounge sofa; not a different apartment.

**ACTION 3 — kneeling POV:** from the man looking down; she kneels on the bar floor in front of him; over-shoulder / first-person; same bar behind them.

## If identity or room slips

- Faces drift: raise ApplyInstantID `ip_weight` from 0.8 toward 1.0, or feed a tight face crop as the InstantID image.
- Sofa rebuilds a new room: raise Depth toward 0.8 or lower denoise to ~0.4.
- Sofa/POV copy the hug pose: keep `image_kps` disconnected (do not wire the master into keypoints).
- POV still looks like the hug framing: raise scene 3 denoise (0.65–0.75) or strengthen POV words in ACTION 3. Do not add Depth on scene 3.

Outputs land in ComfyUI `output/` with the prefixes above.
