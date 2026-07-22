# Deploying MeshPilot — step by step

For deploying MeshPilot to the existing Hetzner server that runs
**agenthinkmesh.com**. No coding needed — copy each command exactly.

**Server:** `88.198.91.81` · **Log in with:** `ssh root@88.198.91.81`

> ### ⚠ Read this first — two things will block you
>
> **1. DNS does not exist yet.** `meshpilot.agenthinkmesh.com` currently points
> nowhere. Until someone creates it (Step 7), the site cannot work — no command
> here can fix that.
>
> **2. Step 8 can take agenthinkmesh.com offline if done wrong.** The web server
> config file on disk is out of date: it is missing the `api.agenthinkmesh.com`
> entry that is currently running. Reloading it as-is would drop the live API.
> Step 8 fixes that first. **Do not skip or reorder Step 8.**

---

## Step 1 — Log in to the server

On your own computer, open a terminal and run:

```bash
ssh root@88.198.91.81
```

Everything from here until Step 9 runs **on the server**.

---

## Step 2 — Check there is room

```bash
free -h && df -h /
```

Look at the **available** column for memory. You need roughly **4 GB free**.
If it is much lower, stop and ask before continuing — MeshPilot shares this
machine with the live website.

---

## Step 3 — Upload the project

From **your own computer** (open a second terminal — do not close the server
one), from the folder containing the `meshpilot` directory:

```bash
scp -r meshpilot root@88.198.91.81:/root/meshpilot-upload
```

Wait for it to finish. It copies a few hundred files.

---

## Step 4 — Set the admin password

Back in the **server** terminal:

```bash
cd /root/meshpilot-upload
read -rsp 'Choose a MeshPilot admin password: ' P && \
  sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=$P|" .env && \
  sed -i "s|^GRAFANA_PASSWORD=.*|GRAFANA_PASSWORD=$P|" .env && \
  sed -i "s|^GRAFANA_ADMIN_PASSWORD=.*|GRAFANA_ADMIN_PASSWORD=$P|" .env && \
  unset P && echo && echo "password set"
```

Your typing stays hidden. Write the password down somewhere safe — it is not
recoverable from here.

---

## Step 5 — Run the installer

```bash
cd /root/meshpilot-upload && sudo bash deploy.sh
```

This installs Docker if missing, copies everything to `/opt/meshpilot`, and
starts the containers. **The first run takes 10–20 minutes** (it compiles
llama.cpp). It is normal for it to look stuck — leave it alone.

It finishes with a green success box. If it stops with a red `✗`, copy the
message and send it over.

---

## Step 6 — Test it locally

```bash
cd /opt/meshpilot && bash test.sh
```

- **PASS** → the engine works. Continue.
- **FAIL** → send the output. Common and expected: no AI model has been
  downloaded yet, so inference has nothing to run. That is a content problem,
  not an install problem.

---

## Step 7 — Create the DNS record

Not on the server — in whoever manages DNS for `agenthinkmesh.com`
(the main site sits behind Cloudflare):

| Field | Value |
|---|---|
| Type | `A` |
| Name | `meshpilot` |
| Value / Target | `88.198.91.81` |
| Proxy status | **DNS only** (grey cloud, not orange) |

The grey cloud matters — with the orange cloud the server cannot obtain its
security certificate.

Wait a few minutes, then check from the server:

```bash
getent hosts meshpilot.agenthinkmesh.com
```

You want to see `88.198.91.81`. If you get nothing, DNS has not spread yet —
wait and try again. **Do not go to Step 8 until this works.**

---

## Step 8 — Connect the web address (the careful step)

This is the step that can break the live site. Do it exactly.

**8a. Back up the current config:**

```bash
cp /opt/pitchproof/infra/Caddyfile /opt/pitchproof/infra/Caddyfile.backup
```

**8b. Add both entries** — the one that restores the live API, and the new
MeshPilot one:

```bash
cat /opt/meshpilot/meshpilot-caddy.conf | grep -v '^#' >> /opt/pitchproof/infra/Caddyfile
```

**8c. Let the web server reach MeshPilot** (one-time):

```bash
cd /opt/pitchproof && \
  grep -q host-gateway docker-compose.yml || \
  sed -i '/^  caddy:/a\    extra_hosts:\n      - "host.docker.internal:host-gateway"' docker-compose.yml && \
  docker compose up -d caddy
```

**8d. Check both sites are alive:**

```bash
curl -s https://api.agenthinkmesh.com/health
curl -sI https://meshpilot.agenthinkmesh.com/ | head -1
```

The first must print `{"status":"ok",...}`. The second should say `200`
(a certificate can take up to a minute on the first try).

> **If `api.agenthinkmesh.com` stops responding, undo immediately:**
> ```bash
> cp /opt/pitchproof/infra/Caddyfile.backup /opt/pitchproof/infra/Caddyfile
> cd /opt/pitchproof && docker compose restart caddy
> ```
> Then send me the output — do not keep trying.

---

## Step 9 — Open it

In a browser: **https://meshpilot.agenthinkmesh.com**

Sign in with `farouq@agenthinkmesh.com` and the password from Step 4.

---

## Step 10 — Everyday commands

Run these from `/opt/meshpilot`:

```bash
cd /opt/meshpilot && docker compose ps          # what is running
cd /opt/meshpilot && docker compose logs -f     # watch logs (Ctrl-C exits)
cd /opt/meshpilot && docker compose restart     # restart MeshPilot
cd /opt/meshpilot && docker compose down        # stop MeshPilot
```

None of these affect agenthinkmesh.com — it is a separate stack.

---

## Memory — please read

This server has **7.6 GB of RAM** and already runs agenthinkmesh.com (~2 GB).
The MeshPilot files as delivered requested **40 GB**; that has been reduced to
fit (`api` 1 GB, `llama.cpp` 3 GB, `onnx` 1 GB, `worker` 768 MB).

Practical consequence: **only small AI models will run here** — roughly a 3
billion parameter model, 4-bit. A 7B model will not fit and will be killed
mid-request. If you need bigger models, this box needs more RAM.

Also: MeshPilot is set to use all 4 CPU cores for inference. While a request is
running, **agenthinkmesh.com may respond more slowly**. If that becomes a
problem, edit `/opt/meshpilot/.env`, set `LLAMA_THREADS=2`, and run
`cd /opt/meshpilot && docker compose up -d`.

---

## If something goes wrong

| Symptom | What to do |
|---|---|
| `deploy.sh` stops with a red `✗` | Send the last 20 lines |
| Site shows "502 Bad Gateway" | `cd /opt/meshpilot && docker compose ps` — something is not running |
| A container keeps restarting | `cd /opt/meshpilot && docker compose logs --tail=50 <name>` |
| Certificate error in the browser | Check Step 7 used the **grey cloud**, then wait 2 minutes |
| **agenthinkmesh.com is down** | Run the rollback in Step 8 immediately |
