# PC Remote

A small Flask dashboard for controlling a computer through a local Python agent. The web app can run on Vercel; the agent must keep running on the computer being controlled. It checks in over HTTPS and only accepts screenshot and allowlisted app-management commands. There is no arbitrary shell or command execution.

## Deploy the dashboard

1. Create an Upstash Redis database and connect it to the Vercel project.
2. Set these environment variables in Vercel:

	- `AGENT_TOKEN`: a different, random secret shared only with the local agent.
	- `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`: the Upstash REST URL and token.

	Generate a random `AGENT_TOKEN` with `openssl rand -hex 32`. Redeploy after adding the variables.
3. Deploy this repository to Vercel. The app uses secure, HTTP-only, same-site session cookies and requires HTTPS.

## Run the PC agent

Install Python 3.10+ on the computer you want to control, then install the dependencies:

```sh
python -m pip install -r requirements.txt
```

Edit the configuration block at the top of `pc_client.py`: set `REMOTE_CONTROL_URL` to the Vercel deployment URL, set `AGENT_TOKEN` to the same token configured in Vercel, and update `ALLOWED_APPS` with the applications you want to control. Values are argument arrays, not shell commands. The agent requests a temporary 3-character pairing code at startup and prints it in the terminal. Enter that code in the web app within five minutes.

```sh
python pc_client.py
```

The script includes a Windows Calculator example. On other systems, replace it with the appropriate executable and add any other apps to `ALLOWED_APPS`. Run the agent in the logged-in desktop session; screenshot capture requires an available graphical display. The dashboard can take screenshots, list configured apps, start allowlisted apps, and close apps started by that agent process. Closing an app is intentionally limited to processes started by the agent.

## Security notes

Keep the agent token private. Since it is configured in `pc_client.py`, do not publish or share that script. Browser sessions are stored in Redis, expire after 12 hours, and are sent in secure, HTTP-only cookies. The pairing code expires after five minutes, and login attempts are rate-limited. Anyone who pairs successfully can view screenshots and launch or close configured applications. Keep the allowlist narrow, use HTTPS, and stop the local agent when remote access is not needed. The command queue and results are held in Upstash Redis with a short expiry; no commands or screenshots are stored on the PC agent's disk.