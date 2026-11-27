<div align="center">

# Kinora

**Turn motion ideas into robot motions you can review, edit, and export.**

Motion generation · Motion library · Robot retargeting · Motion refinement · Agent tools

[Download](#downloads) · [Deploy a server](docs/SERVER.md) · [Use Gadget](docs/GADGET.md) · [All releases](https://github.com/PoirotJin/kinora/releases)

</div>

---

Kinora brings motion generation, search, preview, retargeting, and refinement into one workspace. Connect the desktop client to your team's server, or integrate motion workflows into an existing agent through Gadget's CLI and MCP tools.

## Downloads

**Kinora `1.0.0` (macOS & Ubuntu):** After installation, connect to your Kinora server and sign in. The server handles generation and retargeting.

| Download | Version | Compatibility |
| --- | --- | --- |
| [macOS installer (DMG)](https://github.com/PoirotJin/kinora/releases/download/kinora-v1.0.0/kinora_1.0.0_universal.dmg) | 1.0.0 | Universal, Apple silicon and Intel (macOS 11+) |
| [Ubuntu installer (DEB)](https://github.com/PoirotJin/kinora/releases/download/kinora-v1.0.0/kinora_1.0.0_amd64.deb) | 1.0.0 | Ubuntu 22.04+ x86_64 |
| [Server source (ZIP, includes Gadget)](https://github.com/PoirotJin/kinora/releases/download/kinora-v1.0.0/kinora-server-1.0.0.zip) | 1.0.0 | Full server and compute queue, with Gadget 0.9.1 |
| [Accelerated G1 Retargeting Wheel (Ubuntu)](https://github.com/PoirotJin/kinora/releases/download/kinora-v1.0.0/kinora_retarget_accel-1.0.0-cp310-cp310-linux_x86_64.whl) | 1.0.0 | Optional prebuilt runtime, Ubuntu x86_64 Python 3.10 |
| [Accelerated G1 Retargeting Wheel (macOS)](https://github.com/PoirotJin/kinora/releases/download/kinora-v1.0.0/kinora_retarget_accel-1.0.0-cp313-cp313-macosx_11_0_arm64.whl) | 1.0.0 | Optional prebuilt runtime, macOS arm64 Python 3.13 |

[Release checksums (SHA-256)](https://github.com/PoirotJin/kinora/releases/download/kinora-v1.0.0/SHA256SUMS.txt) · [Installation and updates](docs/INSTALL.md) · [Versions and compatibility](docs/VERSIONS.md)

## Get started

### Use the desktop client

1. Download and install the `.dmg` (macOS) or `.deb` (Ubuntu) package.
2. Enter your team's Kinora server address and sign in with your account.
3. Search and preview motions in the library, then generate, edit, splice, retarget, or refine motions using the features enabled on your server.

### Run your own server

Clone this repository (or download `kinora-server-1.0.0.zip`) and follow the [server quickstart](docs/SERVER.md) and [self-hosting guide](docs/SELF_HOSTING.md).

The `1.0.0` server includes built-in **Unitree G1 (23 DoF)** retargeting (with optional prebuilt accelerated runtime and automatic fallback to the standard offline kinematic solver), custom URDF/MJCF robot import, Kimodo generation for 1–120 second motions, local motion repair, two-clip splicing, scene and multi-actor composition, video-to-motion isolation, NPZ/BVH export, mesh-based editing, dynamics validation, and Gadget `0.9.1`.

### Connect an agent through Gadget

Configure your server URL and Kinora API key to run Gadget via CLI or as a local MCP subprocess:

```bash
export MOTIONFARM_URL="https://your-kinora.example.com"
export MOTIONFARM_API_KEY_FILE="$HOME/.config/kinora/api-key.txt"
python3 motionfarm-gadget/motionfarm.py info
```

See the [Gadget quickstart](docs/GADGET.md) for full setup instructions.

## License

Kinora's own desktop, server, and Gadget code is licensed under the [Apache License 2.0](LICENSE). Third-party components retain their own licenses, including Apache-2.0 for holosoma, MIT for three.js, and SIL OFL 1.1 for EB Garamond. See [NOTICE](NOTICE) for attribution. Separately downloaded models, robot assets, and external services are subject to their respective terms.

Validate motions with the intended robot, controller, and operating environment before hardware use. Automated geometry and dynamics checks do not certify hardware safety.
