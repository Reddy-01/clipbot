# Deploy ClipBot on an Oracle Cloud Always Free VM

This package contains the ClipBot browser dashboard and engine, including prompt-to-video, Piper narration, Whisper captions, FFmpeg, visual analysis, smart cropping, and optional publishing. The Linux build fetches the model files from their original sources and checks their SHA-256 hashes. Existing videos, credentials, and Windows executables are excluded.

## Provision the server

1. Sign in to [Oracle Cloud Free Tier](https://www.oracle.com/cloud/free/). Oracle normally asks for a payment card to verify identity. Stay on the Free Tier and select resources marked **Always Free**. The free VM may be unavailable in your home region, and Oracle may reclaim idle capacity.
2. Create an Ubuntu ARM64 Ampere A1 VM with up to 2 OCPUs and 12 GB RAM within your tenancy's Always Free limits. Enable a public IPv4 address and verify the boot disk falls within the Always Free storage tier. Use SSH public-key authentication. Install [Docker Engine and the Compose plugin for Ubuntu](https://docs.docker.com/engine/install/ubuntu/).
3. Create a DNS A record that points a hostname you control to the VM's public IPv4 address. Use an existing domain or a free DNS hostname if available. Allow inbound TCP 80 and 443 in the OCI network rules and the host firewall if enabled. Limit SSH (TCP 22) to your administrator IP when possible. Port 8765 stays inside the Docker network.

## Start ClipBot

Copy the ZIP to the VM with SCP and extract it. On the VM, run the commands below from the extracted project. Commands assume your user can run Docker.

```sh
cd cloud
umask 077
cp .env.example .env
docker run --rm -it caddy:2-alpine caddy hash-password
```

Set a strong password at the interactive prompt. Edit `.env` with your DNS hostname, username, and the bcrypt hash that Caddy prints. Keep the hash inside single quotes, including its dollar signs.

```sh
docker compose config --quiet
docker compose up --build -d
docker compose ps
docker compose logs --tail=60
```

The first build compiles the CPU-only script runner and downloads the models. When DNS and TLS are ready, open `https://YOUR_HOSTNAME` in a browser on any device and enter your username and password. Use **Create from Prompt** for a short initial test. Rendered videos live in the `clipbot_data` Docker volume, which also stores imports and settings. Keep a backup of this volume. Omit the volume flag when running `docker compose down` to preserve it.

## Publishing and existing data

- Local rendering and preview work without a social account. For YouTube, use a Google OAuth **Web application** client with `https://YOUR_HOSTNAME/api/oauth/callback` as the authorized redirect URI. Upload only the new OAuth client configuration as `/data/oauth_client.json`, then click **Connect channel** in ClipBot. Google may require domain verification, test-user configuration, or further app review. Keep OAuth files out of the source archive.
- Postiz is optional. If you already run Postiz, fill in the URL and key in the VM's `.env` file. Review draft destinations before scheduling or publishing.
- From Windows, transfer only the `input` and `output` folders you want to migrate. Keep their subfolder layout. The container runs as UID/GID `10001:10001`, so migrated files need to be readable and exports directories writable by that account. Reconnect publishing credentials separately.
- Cloud data-center IPs can face YouTube download restrictions. If a permitted link import fails, upload your local MP4 from the browser.
