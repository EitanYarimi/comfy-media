# Same-room couple — three scenes

Import [`same-room-couple.json`](same-room-couple.json) in ComfyUI (**Load** / drag onto the canvas). It is a fork of your Pony txt2img graph (`cyberrealisticPony_v127Alt` + **Pony Realism Slider** 1.8, 1216×832, `dpmpp_2m` / karras, 20 steps, CFG 2.5).

One queue writes three stills of the **same two adults in the same dark bar**:

| File prefix | Scene | How it is sampled |
|---|---|---|
| `scene1_hug_bar` | Hugging at the bar counter | txt2img, denoise **1.0** (master) |
| `scene2_sofa` | Sitting on the lounge sofa in that bar | img2img from scene 1, denoise **0.50**, Depth **0.65** |
| `scene3_kneeling_pov` | He looks at her; she kneeling; his POV | img2img from scene 1, denoise **0.60**, **no Depth** — POV from the checkpoint prompt |

The depth preprocessor is **only for scene 2** (same sofa/bar layout). Scene 3 does not use it: a depth map of the hug freeze the original camera and fights POV. Your Pony checkpoint already understands POV from the ACTION prompt. **IPAdapter** still copies the same faces from the master.

## Custom nodes and models

Install with ComfyUI Manager:

- [ComfyUI_IPAdapter_plus](https://github.com/cubiq/ComfyUI_IPAdapter_plus)
- [comfyui_controlnet_aux](https://github.com/Fannovel16/comfyui_controlnet_aux)

Then put matching weights in `models/` (names vary; pick yours in the dropdowns):

| Node | Typical files |
|---|---|
| Checkpoint | `Copy of Copy of Copy of cyberrealisticPony_v127Alt.safetensors` (whatever you already use) |
| LoRA | `Pony Realism Slider.safetensors` |
| IPAdapter Unified Loader preset **PLUS (high strength)** | `ip-adapter-plus_sdxl_vit-h.safetensors` + CLIP Vision `CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors` |
| Load ControlNet | SDXL depth for **scene 2 only**, e.g. `diffusers_xl_depth_full.safetensors` |

If a node shows red after import, select the file you actually have. Pony is SDXL-shaped, so use **SDXL** IPAdapter and ControlNet, not SD1.5.

## Queue order

1. **Queue Prompt.** Scene 1 generates the master hug; scenes 2 and 3 are built from that image in the same run.
2. Keep generating until the couple and bar look right. Then set scene 1’s seed to **fixed**.
3. To reuse a saved PNG: load it in **OPTIONAL: load frozen master PNG**, then reconnect that `IMAGE` output to **IPAdapter**, **Depth preprocessor**, and **VAEEncode** (disconnect those three from scene 1 decode). Mute scene 1’s KSampler if you do not want a new hug.

Do **not** edit **LOOK**, **ROOM**, or **PEOPLE**. Only the three **ACTION** boxes change pose/camera.

## Frozen vs action prompts

**ROOM (frozen):** dark bar at night, counter, bottles, dim practicals, lounge sofa on the wall in the *same* bar.

**PEOPLE (frozen):** same young woman (club dress) and same older man (suit).

**ACTION 1 — hug:** both standing at the bar, embracing, side/three-quarter, full body.

**ACTION 2 — sofa:** both sitting on that bar’s lounge sofa; not a different apartment.

**ACTION 3 — kneeling POV:** from the man looking down; she kneels on the bar floor in front of him; over-shoulder / first-person; same bar behind them.

Clothes stay in PEOPLE unless you change that box on purpose.

## If identity or room slips

- Faces drift: raise IPAdapter weight (start ~0.75, try 0.85) or switch the Unified Loader preset to **PLUS FACE**.
- Sofa scene rebuilds a new room: raise Depth strength toward 0.8 or lower denoise to ~0.4.
- POV still looks like the hug framing: raise scene 3 denoise slightly (0.65–0.75) or strengthen POV words in ACTION 3 (`first person`, `from his eyes`, `looking down`). Do not add Depth back unless you want the original hug camera.
- Extra people or a child in frame: already in the shared negative; add `1girl, 1boy, two people only` to PEOPLE if needed.

Outputs land in ComfyUI `output/` with the prefixes above, which this gallery already browses if that folder is your media root.
