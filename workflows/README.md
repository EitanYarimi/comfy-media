# Same-room couple — three scenes

Import [`same-room-couple.json`](same-room-couple.json) in ComfyUI (**Load** / drag onto the canvas). It is a fork of your Pony txt2img graph (`cyberrealisticPony_v127Alt` + **Pony Realism Slider** 1.8, 1216×832, `dpmpp_2m` / karras, 20 steps, CFG 2.5).

Identity uses InstantID like `linkedin_instantid_00427_.json`, but **ControlNet strength is 0.2** (LinkedIn used 0.8). At 0.8 InstantID copies the hug pose onto every scene, so all three images look the same.

All three scenes are **txt2img** (empty latent, denoise **1.0**). Hug-depth img2img is muted. ACTION is concatenated **first**. Sofa/POV have extra negatives that ban hugging / side two-shots.

| File prefix | Scene | How it is sampled |
|---|---|---|
| `scene1_hug_bar` | Standing hug at the bar | txt2img |
| `scene2_sofa` | Both **sitting** on the lounge sofa | txt2img + InstantID faces (cn **0.2**) |
| `scene3_kneeling_pov` | **POV** looking down, she kneeling | txt2img + InstantID faces (cn **0.2**) |

ApplyInstantIDAdvanced: `ip 0.85 / cn 0.2 / start 0.55 / concat`. `image_kps` unconnected. InstantID `image` is the scene 1 still (faces only at this CN).

## Custom nodes and models

Install with ComfyUI Manager:

- [ComfyUI_InstantID](https://github.com/cubiq/ComfyUI_InstantID) (same pack as the LinkedIn workflow)
- [comfyui_controlnet_aux](https://github.com/Fannovel16/comfyui_controlnet_aux) (optional; hug-depth nodes are **muted**)

| Node | File / setting |
|---|---|
| Checkpoint | `Copy of Copy of Copy of cyberrealisticPony_v127Alt.safetensors` |
| LoRA | `Pony Realism Slider.safetensors` |
| InstantID Model | `models/instantid/ip-adapter.bin` |
| InstantID ControlNet | `instantid_controlnet.safetensors` |
| InsightFace | `models/insightface/models/antelopev2/*.onnx` |
| Face Analysis | **CUDA** |
| InstantID weights | ip **0.85**, cn **0.2** (raise cn only if you want pose copied) |

InstantID is built for **one primary face**. On a two-person still it usually locks the largest/closest face. For a dedicated identity photo, load it in **OPTIONAL FACE REFERENCE** and reconnect that `IMAGE` into both ApplyInstantID `image` inputs.

## Queue order

1. **Queue Prompt.** All three txt2img in one run. Scene 1 still feeds InstantID `image` for faces on 2 and 3.
2. If sofa/POV still look like the hug: InstantID `cn_strength` is too high — keep it at **0.2** or **0**.
3. Faces too weak: raise `ip_weight` toward 1.0, or reconnect a close-up into ApplyInstantID `image`.

Do **not** edit **LOOK**, **ROOM**, or **PEOPLE**. Only the three **ACTION** boxes change pose/camera.

## Frozen vs action prompts

**ROOM (frozen):** dark bar at night, counter, bottles, dim practicals, lounge sofa on the wall in the *same* bar.

**PEOPLE (frozen):** same young woman (club dress) and same older man (suit).

**ACTION 1 — hug:** both standing at the bar, embracing, side/three-quarter, full body.

**ACTION 2 — sofa:** both sitting on that bar’s lounge sofa; not a different apartment.

**ACTION 3 — kneeling POV:** from the man looking down; she kneels on the bar floor in front of him; over-shoulder / first-person; same bar behind them.

## Colab errors

- **`IPAdapter model not found`** — old graph. Re-import this JSON. InstantID does not use `models/ipadapter/`. It uses `models/instantid/ip-adapter.bin` (same as your LinkedIn workflow).
- **`Failed to find ... Depth-Anything-V2-Large` / `/tmp/ckpts`** — do not use DepthAnythingV2 on Colab. This file’s sofa preprocessor is MiDaS.

## If identity or room slips

- Faces drift: raise ApplyInstantID `ip_weight` from 0.8 toward 1.0, or feed a tight face crop as the InstantID image.
- Sofa rebuilds a new room: raise Depth toward 0.8 or lower denoise to ~0.4.
- Sofa/POV copy the hug pose: keep `image_kps` disconnected (do not wire the master into keypoints).
- POV still looks like the hug framing: raise scene 3 denoise (0.65–0.75) or strengthen POV words in ACTION 3. Do not add Depth on scene 3.

Outputs land in ComfyUI `output/` with the prefixes above.
