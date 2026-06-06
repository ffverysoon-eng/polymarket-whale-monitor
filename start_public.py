#!/usr/bin/env python3
"""
Start cloudflared tunnel and update GitHub Pages redirect URL automatically.
Usage: python3 start_public.py
"""
import subprocess, re, time, urllib.request, json, base64, sys, os

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_USER  = "ffverysoon-eng"
GITHUB_REPO  = "polymarket-whale-monitor"
CLOUDFLARED  = os.path.expanduser("~/.local/bin/cloudflared")
PAGES_FILE   = "docs/index.html"

def github_api(method, path, data=None):
    url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method,
        headers={"Authorization": f"token {GITHUB_TOKEN}",
                 "User-Agent": "tunnel-updater",
                 "Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        print(f"GitHub API error: {e.read().decode()}")
        return None

def update_github_pages(tunnel_url):
    print(f"Updating GitHub Pages redirect → {tunnel_url}")
    existing = github_api("GET", f"contents/{PAGES_FILE}")
    if not existing:
        print("Could not fetch existing file")
        return False

    sha = existing["sha"]
    current_content = base64.b64decode(existing["content"]).decode()

    # Replace any existing tunnel URL or placeholder
    new_content = re.sub(
        r'(content="0; url=)[^"]+(")',
        rf'\g<1>{tunnel_url}\2',
        current_content
    )
    new_content = re.sub(
        r'(href=")[^"]+(" style)',
        rf'\g<1>{tunnel_url}\2',
        new_content
    )
    # Also handle the <a href> line
    new_content = re.sub(
        r'(<a href=")[^"]+(">[Cc]lick)',
        rf'\g<1>{tunnel_url}\2',
        new_content
    )

    encoded = base64.b64encode(new_content.encode()).decode()
    result = github_api("PUT", f"contents/{PAGES_FILE}", {
        "message": f"update tunnel url to {tunnel_url}",
        "content": encoded,
        "sha": sha
    })
    return result is not None

def start_tunnel():
    print("Starting Cloudflare tunnel...")
    proc = subprocess.Popen(
        [CLOUDFLARED, "tunnel", "--url", "http://localhost:5050"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )

    tunnel_url = None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
        if match:
            tunnel_url = match.group(0)
            break

    if not tunnel_url:
        print("ERROR: Could not detect tunnel URL")
        proc.terminate()
        return

    print(f"\n✓ Tunnel URL: {tunnel_url}")
    print(f"  GitHub Pages: https://{GITHUB_USER}.github.io/{GITHUB_REPO}/\n")

    if update_github_pages(tunnel_url):
        print("✓ GitHub Pages redirect updated (takes ~30s to propagate)")
    else:
        print("✗ GitHub Pages update failed — share this URL directly:")
        print(f"  {tunnel_url}")

    print("\nPress Ctrl+C to stop the tunnel.\n")
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        print("\nTunnel stopped.")

if __name__ == "__main__":
    start_tunnel()
