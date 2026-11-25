<div align="center">

# Kinora

**Turn motion ideas into robot motions you can review, edit, and export.**

Motion generation · Motion library · Robot retargeting · Motion refinement · Agent tools

[Download](#downloads) · [Deploy a server](docs/SERVER.md) · [Use Gadget](docs/GADGET.md) · [All releases](https://github.com/PoirotJin/kinora/releases)

</div>

---

Kinora brings motion generation, search, preview, retargeting, and refinement into one workspace. Connect the desktop client to your team's server, or integrate motion workflows into an existing agent through Gadget's CLI and MCP tools.

## Downloads

**1.0.0-beta.3 prereleases:**
- **macOS beta:** [Kinora 1.0.0-beta.3 universal DMG](https://github.com/PoirotJin/kinora/releases/download/kinora-v1.0.0-beta.3/kinora_1.0.0-beta.3_universal.dmg) · [Release notes](https://github.com/PoirotJin/kinora/releases/tag/kinora-v1.0.0-beta.3) · [SHA-256 checksums](https://github.com/PoirotJin/kinora/releases/download/kinora-v1.0.0-beta.3/SHA256SUMS.txt)
- **Ubuntu beta:** [Kinora 1.0.0-beta.3 amd64 DEB](https://github.com/PoirotJin/kinora/releases/download/kinora-v1.0.0-beta.3-ubuntu/kinora_1.0.0-beta.3_amd64.deb) · [Release notes](https://github.com/PoirotJin/kinora/releases/tag/kinora-v1.0.0-beta.3-ubuntu) · [SHA-256 checksums](https://github.com/PoirotJin/kinora/releases/download/kinora-v1.0.0-beta.3-ubuntu/SHA256SUMS.txt)
- **Server beta:** [Kinora Server 1.0.0-beta.3 ZIP](https://github.com/PoirotJin/kinora/releases/download/kinora-v1.0.0-beta.3-server/kinora-server-1.0.0-beta.3.zip) · [Release notes](https://github.com/PoirotJin/kinora/releases/tag/kinora-v1.0.0-beta.3-server) · [SHA-256 checksums](https://github.com/PoirotJin/kinora/releases/download/kinora-v1.0.0-beta.3-server/SHA256SUMS.txt)

The `1.0.0-beta.3` desktop betas include unified multi-tab workspace, video-to-motion isolation, batch export for personal assets, task recovery, robot edit failure rollback, and retargeting for edited, spliced, and scene results. The macOS universal DMG supports Apple silicon and Intel Macs on macOS 11+ (ad-hoc signed, not Apple notarized); the Ubuntu DEB targets Ubuntu 22.04+ x86_64. The `1.0.0-beta.3` server ZIP bundles Gadget `0.9.1`, `deploy/` (`nginx.conf`), `deployment/`, and `scripts/` runtime tools. Available features depend on the connected server. These prereleases are distributed separately from the stable update feed.

The stable desktop installers remain available below. After installation, connect to your Kinora server and sign in. The server handles generation and retargeting.

| Download | Version | Compatibility |
| --- | --- | --- |
| [macOS installer (DMG)](https://github.com/PoirotJin/kinora/releases/download/kinora-v0.7.0/kinora_0.7.0_universal.dmg) | 0.7.0 | Universal, Apple silicon and Intel |
| [Windows installer (EXE)](https://github.com/PoirotJin/kinora/releases/download/kinora-v0.7.0/kinora_0.7.0_x64-setup.exe) | 0.7.0 | x86_64 |
| [Linux installer (DEB)](https://github.com/PoirotJin/kinora/releases/download/kinora-v0.7.0/kinora_0.7.0_amd64.deb) | 0.7.0 | Ubuntu 22.04+ or Debian 12+, x86_64 |
| [Server source (ZIP, includes Gadget)](https://github.com/PoirotJin/kinora/releases/download/kinora-server-v0.5.2/kinora-server-0.5.2.zip) | 0.5.2 | Full server and compute queue, with Gadget 0.15.2 |
| [Gadget (ZIP)](https://github.com/PoirotJin/kinora/releases/download/kinora-v0.5.1/kinora-gadget-0.15.2.zip) | 0.15.2 | Python 3.10+, CLI and MCP |

[Stable desktop checksums](https://github.com/PoirotJin/kinora/releases/download/kinora-v0.7.0/SHA256SUMS.txt) · [Server checksums](https://github.com/PoirotJin/kinora/releases/download/kinora-server-v0.5.2/SHA256SUMS.txt) · [Gadget checksums](https://github.com/PoirotJin/kinora/releases/download/kinora-v0.5.1/SHA256SUMS.txt) · [Installation and updates](docs/INSTALL.md) · [Versions and compatibility](docs/VERSIONS.md)

## Get started

### Use the desktop client

1. Download and install the package for your operating system.
2. Enter your team's Kinora server address. Sign in with an existing account or choose **Request Account** to open the application form in your browser. On servers that require approval, an administrator activates the account without email verification.
3. Search and preview motions in the library, then generate, retarget, or refine motions using the features enabled on your server.

Desktop 0.7.0 supports 3D robot mesh previews, joint adjustments, keyframe drafts, and comparisons with the original motion when the server enables those features. Review and explicitly accept a candidate before exporting it as an accepted result.

### Run your own server

Download the `1.0.0-beta.3` (or stable `0.5.2`) server source ZIP and follow the [server quickstart](docs/SERVER.md) to run the local service. The [self-hosting guide](docs/SELF_HOSTING.md) covers compute setup, model weights, robot assets, video extraction setup, and a team HTTPS endpoint.

The `1.0.0-beta.3` server supports Kimodo generation for 1–120 second motions, local motion repair, two-clip splicing, scene and multi-actor composition, video-to-motion isolation, NPZ and BVH export, custom robots, retargeting, mesh-based editing, and dynamics validation. The package includes account pages, Gadget `0.9.1`, `deploy/nginx.conf`, and server/acceptance scripts (`scripts/review_server.py`, `scripts/production_supervisor.py`, `scripts/validate_kimodo_edit.py`). Set up the compute environment to run the full motion workflows.

Server 0.5.2 passed 159 regression tests, targeted checks on the final code, a first-launch test of the packaged server, 49 Gadget tests, seven GPU cases, and a 30-frame G1 retargeting check. It also reduced foot drift in the original 30-degree turn-splice case from approximately 14.3 cm to 4.6 cm. Additional seeds, left turns, and 60-degree turns passed, and candidates above the existing 8 cm threshold are quarantined. See the [0.5.2 fix and validation](docs/FOOT_CONTACT_0.5.2.md) and the [0.5.1 validation baseline](docs/RELEASE_VALIDATION.md).

### Connect an agent through Gadget

Download Gadget and configure your server URL and Kinora API key. Run it directly from the command line, or use it as a local MCP subprocess with a client such as Codex or Cursor. Gadget uses your Kinora API key and needs no separate model or agent-client credentials.

```bash
export MOTIONFARM_URL="https://your-kinora.example.com"
export MOTIONFARM_API_KEY_FILE="$HOME/.config/kinora/api-key.txt"
python3 motionfarm-gadget/motionfarm.py info
```

Replace the example URL with your server address and store your key in the configured file. See the [Gadget quickstart](docs/GADGET.md) for full setup instructions.

## Releases and source code

This repository hosts Kinora's public downloads and documentation. The named server ZIP in Releases contains the server source. GitHub's automatically generated source archives contain the files in this public repository. The server source package excludes model weights, production databases, user motions, and account credentials.

See [versions and compatibility](docs/VERSIONS.md) for package details and the [publishing guide](docs/PUBLISHING.md) for maintainer instructions.

## License

Kinora's own desktop, server, and Gadget code is licensed under the [Apache License 2.0](LICENSE). Third-party components retain their own licenses, including Apache-2.0 for holosoma, MIT for three.js, and SIL OFL 1.1 for EB Garamond. See [NOTICE](NOTICE) for attribution. Separately downloaded models, robot assets, and external services are subject to their respective terms. Preserve LICENSE, NOTICE, and bundled third-party license files when redistributing the software.

Validate motions with the intended robot, controller, and operating environment before hardware use. Automated geometry and dynamics checks do not certify hardware safety.
