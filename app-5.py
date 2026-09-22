#!/usr/bin/env python3
"""
Novel Writer — Web Version
---------------------------
Upload an unfinished novel + a target word count. A daily background job
drafts the next chapter (in your voice, continuing from what you've already
written) and emails it to you, until the manuscript hits your target length.

Uses Google's Gemini API free tier for generation — no credit card, no
expiration, and its daily quota is far more than this app needs (1 request/day).
Note: on the free tier, Google may use your prompts to improve their products,
since it isn't a paid/private tier. Worth knowing before feeding it your work.

Env vars required (set these in Render's dashboard, or a local .env file):
    GEMINI_API_KEY      - free from aistudio.google.com, no card needed
    SMTP_HOST           - e.g. smtp.gmail.com
    SMTP_PORT           - e.g. 587
    SMTP_USER           - the email address it sends FROM
    SMTP_PASS           - app password for that email account
    SMTP_TO             - default "send to" address (can be overridden per project)
    APP_PASSWORD         - REQUIRED. Password that protects every page/route
                            of this app (HTTP Basic Auth). Pick something long;
                            this is the only thing standing between the open
                            internet and your manuscripts + publishing tokens.

Optional:
    FLASK_DEBUG          - set to "true" to enable Flask's debugger locally.
                            Never set this in production (Render) — the
                            interactive debugger lets anyone who triggers an
                            error run arbitrary code on the server.

Run locally:
    pip install -r requirements.txt
    export $(cat .env | xargs)   # or set the vars manually
    python app.py
    # visit http://localhost:5000  (you'll be prompted for the APP_PASSWORD)
"""

import os
import json
import time
import shutil
import threading
import smtplib
import traceback
import uuid
from functools import wraps
from email.mime.text import MIMEText
from pathlib import Path

from flask import Flask, request, render_template_string, redirect, url_for, Response
from apscheduler.schedulers.background import BackgroundScheduler
import google.generativeai as genai

APP_DIR = Path(__file__).parent
PROJECTS_DIR = APP_DIR / "projects"
PROJECTS_DIR.mkdir(exist_ok=True)

MODEL = "gemini-3.6-flash"  # current free-tier model (2.5-flash was retired by Google)

app = Flask(__name__)
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
gemini_model = genai.GenerativeModel(MODEL)


# ---------- auth ----------
# Single shared password (HTTP Basic Auth) protecting every route. This is a
# personal tool, not a multi-user product, so one password is enough — but
# without it, anyone who finds the Render URL (or guesses a project id) can
# read your manuscript, trigger generation, or publish under your name.

def _check_auth(password: str) -> bool:
    expected = os.environ.get("APP_PASSWORD")
    return bool(expected) and password == expected


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not _check_auth(auth.password):
            return Response(
                "Authentication required.", 401,
                {"WWW-Authenticate": 'Basic realm="Novel Writer"'}
            )
        return f(*args, **kwargs)
    return decorated


@app.before_request
def _enforce_app_password_configured():
    if not os.environ.get("APP_PASSWORD"):
        return ("Server misconfigured: APP_PASSWORD is not set. "
                "Set it as an environment variable before using this app.", 500)


# ---------- generation (with retries — Gemini calls fail sometimes) ----------

class GenerationError(Exception):
    pass


def generate(prompt: str, max_tokens: int, attempts: int = 3) -> str:
    """Thin wrapper so the rest of the code doesn't care which provider is used.
    Retries transient failures (timeouts, rate limits, empty responses) with
    backoff instead of letting one flaky call kill a whole run."""
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            resp = gemini_model.generate_content(
                prompt,
                generation_config=genai.types.GenerationConfig(max_output_tokens=max_tokens),
            )
            text = (resp.text or "").strip()
            if not text:
                raise GenerationError("Model returned empty text")
            return text
        except Exception as e:  # noqa: BLE001 - deliberately broad, this is a retry boundary
            last_err = e
            if attempt < attempts:
                time.sleep(5 * attempt)  # 5s, 10s backoff
    raise GenerationError(f"Generation failed after {attempts} attempts: {last_err}")


# ---------- storage helpers ----------

def project_path(pid: str) -> Path:
    return PROJECTS_DIR / pid

def load_project(pid: str) -> dict:
    with open(project_path(pid) / "state.json", "r", encoding="utf-8") as f:
        return json.load(f)

def save_project(pid: str, state: dict):
    with open(project_path(pid) / "state.json", "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def word_count(text: str) -> int:
    return len(text.split())

def all_projects() -> list:
    return [p.name for p in PROJECTS_DIR.iterdir() if p.is_dir()]


# ---------- generation ----------

def extract_style_guide(manuscript_text: str) -> str:
    prompt = f"""Below is an excerpt from a novel manuscript. Analyze the author's voice
closely and produce a detailed STYLE GUIDE another writer could use to continue this
manuscript convincingly in the same voice.

Cover: sentence rhythm, vocabulary, dialogue style, pacing, POV/tense, recurring
imagery, tone, and how they handle scene transitions.

MANUSCRIPT EXCERPT:
{manuscript_text[:40000]}

Respond with only the style guide."""
    return generate(prompt, max_tokens=1500)


def generate_next_chapter(state: dict) -> str:
    prompt = f"""You are continuing a novel manuscript for its original author, matching
their voice exactly.

STYLE GUIDE:
{state['style_guide']}

NOVEL BIBLE / DIRECTION (if provided):
{state.get('bible', 'None provided — infer direction from the manuscript itself.')}

STORY SO FAR (summary of everything written to this point):
{state.get('running_summary', 'This is the continuation of an existing manuscript — see the excerpt below.')}

LAST ~1500 WORDS OF THE MANUSCRIPT (continue directly from here, do not repeat it):
{state['manuscript_tail']}

Write the next chapter. Match the style guide precisely. Advance the plot
naturally from where the manuscript leaves off, following the bible's outline
for this point in the story. Write a complete chapter, not a summary — this
chapter must be AT LEAST 1000 words. Do not stop early or wrap up quickly;
develop the scene fully with description, dialogue, and pacing that matches
the style guide, until you've genuinely reached that length.

Respond with only the chapter text, starting with a chapter heading."""
    chapter = generate(prompt, max_tokens=6000)

    # Safety net: if the model still came in short, ask it to expand once.
    if word_count(chapter) < 1000:
        expand_prompt = f"""This chapter draft is too short ({word_count(chapter)} words). Expand it to
at least 1000 words by deepening description, dialogue, and pacing in the
same voice — do not simply pad with filler, add real scene development.

CURRENT DRAFT:
{chapter}

Respond with only the expanded chapter text."""
        chapter = generate(expand_prompt, max_tokens=6000)

    return chapter


def summarize_chapter(chapter_text: str) -> str:
    prompt = f"""Summarize this chapter in 4-6 sentences: plot developments, character
decisions, new facts a writer needs to remember for continuity. No style commentary.

CHAPTER:
{chapter_text}

Respond with only the summary."""
    return generate(prompt, max_tokens=300)


def publish_to_wordpress(title: str, content_html: str) -> bool:
    """Auto-publish a chapter as a post on a WordPress.com site, if configured.
    Returns True if published, False if WordPress isn't set up (safe no-op)."""
    site = os.environ.get("WP_SITE")          # e.g. "yourblog.wordpress.com"
    token = os.environ.get("WP_ACCESS_TOKEN")
    if not site or not token:
        return False

    import requests
    url = f"https://public-api.wordpress.com/rest/v1.1/sites/{site}/posts/new"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            data={"title": title, "content": content_html, "status": "publish"},
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"WordPress publish failed: {e}")
        return False
    if resp.status_code >= 300:
        print(f"WordPress publish failed: {resp.status_code} {resp.text}")
        return False
    return True


def publish_to_facebook(message: str, image_url: str = None) -> bool:
    """Auto-post a chapter to a Facebook Page, if configured. Posts as a photo
    if an image_url is given, otherwise a plain text post."""
    page_id = os.environ.get("FB_PAGE_ID")
    token = os.environ.get("FB_PAGE_ACCESS_TOKEN")
    if not page_id or not token:
        return False

    import requests
    try:
        if image_url:
            url = f"https://graph.facebook.com/{page_id}/photos"
            resp = requests.post(url, data={"url": image_url, "caption": message, "access_token": token}, timeout=30)
        else:
            url = f"https://graph.facebook.com/{page_id}/feed"
            resp = requests.post(url, data={"message": message, "access_token": token}, timeout=30)
    except Exception as e:  # noqa: BLE001
        print(f"Facebook publish failed: {e}")
        return False

    if resp.status_code >= 300:
        print(f"Facebook publish failed: {resp.status_code} {resp.text}")
        return False
    return True


COPYRIGHT_NOTICE = "\n\n---\nCopyright Azubuike C. Obiora — Mythbborrn Press"


def add_copyright(chapter_text: str) -> str:
    return chapter_text.rstrip() + COPYRIGHT_NOTICE


def generate_image_prompt(chapter_text: str) -> str:
    """One small call: turn a chapter into a short visual description for an image generator."""
    prompt = f"""Read this chapter and write ONE vivid, concrete visual description (under 30 words)
of its most striking scene or image — suitable as a prompt for an AI image generator.
No character names the generator wouldn't recognize; describe what's visually happening instead.

CHAPTER:
{chapter_text[:3000]}

Respond with only the visual description, nothing else."""
    return generate(prompt, max_tokens=100).strip()


def pollinations_image_url(image_prompt: str) -> str:
    """Free, no-key image generation via Pollinations.ai."""
    import urllib.parse
    encoded = urllib.parse.quote(image_prompt)
    return f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=768"


# ---------- email ----------

def send_email(to_addr: str, subject: str, body: str):
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    if not all([host, user, pw, to_addr]):
        print("SMTP not fully configured — skipping email, chapter still saved to disk.")
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(user, pw)
            server.sendmail(user, [to_addr], msg.as_string())
    except Exception as e:  # noqa: BLE001
        print(f"Email send failed (chapter is still saved to disk): {e}")


def send_alert_email(to_addr: str, subject: str, error_text: str):
    """Best-effort notification when a background run fails, so a failure
    doesn't just go silent. Never raises — this is a last resort, not
    something that should itself crash the caller."""
    try:
        send_email(to_addr, subject, error_text)
    except Exception:  # noqa: BLE001
        pass


# ---------- concurrency guard ----------
# One lock per project id. Any code path that generates/publishes a chapter
# (daily cron, manual "run now", "finish all") must hold this lock, so two
# triggers on the same project can never race each other.

_busy_lock = threading.Lock()
_busy_pids = set()


class ProjectBusy(Exception):
    pass


class project_guard:
    """Non-blocking per-project lock. Raises ProjectBusy if already held."""

    def __init__(self, pid: str):
        self.pid = pid

    def __enter__(self):
        with _busy_lock:
            if self.pid in _busy_pids:
                raise ProjectBusy(self.pid)
            _busy_pids.add(self.pid)
        return self

    def __exit__(self, exc_type, exc, tb):
        with _busy_lock:
            _busy_pids.discard(self.pid)
        return False


def is_busy(pid: str) -> bool:
    with _busy_lock:
        return pid in _busy_pids


# ---------- shared: generate + optionally publish one chapter ----------

def write_and_publish_one_chapter(pid: str, state: dict, auto_publish: bool = True) -> dict:
    """Generates the next chapter for a project, saves and emails it always,
    and publishes to WordPress/Facebook only if auto_publish is True.
    Does not check word-count target — caller does."""
    chapter = generate_next_chapter(state)
    chapter = add_copyright(chapter)
    state["manuscript"] += "\n\n" + chapter
    state["manuscript_tail"] = state["manuscript"][-6000:]
    summary = summarize_chapter(chapter)
    state["running_summary"] = (state.get("running_summary", "") + "\n\n" + summary).strip()
    state["chapters_written"] = state.get("chapters_written", 0) + 1
    state.setdefault("published_chapters", [])

    new_word_count = word_count(state["manuscript"])
    save_project(pid, state)

    chap_num = state["chapters_written"]
    (project_path(pid) / f"chapter_{chap_num:03d}.txt").write_text(chapter, encoding="utf-8")

    subject = f"'{state['title']}' — Chapter {chap_num} ({new_word_count}/{state['target_words']} words)"
    send_email(state["email"], subject, chapter)

    if auto_publish:
        publish_chapter_now(pid, state, chap_num)
    else:
        print(f"[{pid}] wrote chapter {chap_num} (draft, not published), {new_word_count} total words")

    return state


def publish_chapter_now(pid: str, state: dict, chap_num: int) -> dict:
    """Publishes an already-written chapter to WordPress/Facebook and marks
    it as published in project state. Safe to call any time after the
    chapter file exists."""
    chap_file = project_path(pid) / f"chapter_{chap_num:03d}.txt"
    chapter = chap_file.read_text(encoding="utf-8")

    try:
        image_prompt = generate_image_prompt(chapter)
        image_url = pollinations_image_url(image_prompt)
    except GenerationError as e:
        print(f"[{pid}] image prompt generation failed, publishing without image: {e}")
        image_url = None

    img_tag = f'<img src="{image_url}" alt="Chapter {chap_num} cover"><br><br>' if image_url else ""
    wp_published = publish_to_wordpress(
        f"{state['title']} — Chapter {chap_num}",
        img_tag + chapter.replace("\n", "<br>")
    )
    fb_published = publish_to_facebook(
        f"{state['title']} — Chapter {chap_num} is up!\n\n{chapter[:500]}...",
        image_url=image_url
    )
    status_note = []
    if wp_published: status_note.append("WordPress")
    if fb_published: status_note.append("Facebook")
    note = f" (published to: {', '.join(status_note)})" if status_note else " (nothing configured to publish to)"
    print(f"[{pid}] published chapter {chap_num}{note}")

    state.setdefault("published_chapters", [])
    if chap_num not in state["published_chapters"]:
        state["published_chapters"].append(chap_num)
    save_project(pid, state)
    return state


# ---------- the daily job ----------

def run_daily_job():
    for pid in all_projects():
        try:
            state = load_project(pid)
        except FileNotFoundError:
            continue

        if state.get("status") != "active":
            continue

        if is_busy(pid):
            print(f"[{pid}] skipping daily run — a job is already in progress for this project")
            continue

        current_words = word_count(state["manuscript"])
        if current_words >= state["target_words"]:
            state["status"] = "complete"
            save_project(pid, state)
            send_email(state["email"], f"'{state['title']}' is complete",
                       f"Your manuscript reached {current_words} words. "
                       f"Final file is saved server-side under project {pid}.")
            continue

        try:
            with project_guard(pid):
                write_and_publish_one_chapter(pid, state)
        except ProjectBusy:
            print(f"[{pid}] skipping daily run — became busy")
        except GenerationError as e:
            print(f"[{pid}] daily chapter generation failed: {e}")
            send_alert_email(
                state["email"],
                f"'{state['title']}' — today's chapter failed",
                f"Today's automatic chapter generation failed with this error:\n\n{e}\n\n"
                f"Nothing was published. It will try again on the next scheduled run, "
                f"or you can trigger it manually from the project page."
            )
        except Exception as e:  # noqa: BLE001 - last-resort guard so one bad project doesn't kill the whole cron run
            print(f"[{pid}] unexpected error in daily job: {e}\n{traceback.format_exc()}")
            send_alert_email(
                state["email"],
                f"'{state['title']}' — today's run hit an unexpected error",
                f"{e}\n\nNothing was published; your manuscript is unchanged."
            )


scheduler = BackgroundScheduler()
scheduler.add_job(run_daily_job, "cron", hour=6)  # runs once daily at 06:00 server time
scheduler.start()


# ---------- external trigger (for free-tier hosting) ----------
# Render's free plan spins the app down after inactivity, so the internal
# 06:00 cron above only fires if something happens to be awake at 06:00 —
# not reliable on free. This endpoint lets an external, free scheduler
# (e.g. cron-job.org, UptimeRobot) hit this URL once a day: the request
# itself wakes the app AND runs the same job the internal cron would have.
# Protected by CRON_SECRET (separate from APP_PASSWORD) since automated
# schedulers usually can't do a login prompt.

@app.route("/cron/run-daily")
def external_cron_trigger():
    secret = os.environ.get("CRON_SECRET")
    if not secret or request.args.get("key") != secret:
        return "Forbidden", 403
    thread = threading.Thread(target=run_daily_job, daemon=True)
    thread.start()
    return "Daily job triggered.", 200


# ---------- "finish it all now" background job ----------

def finish_novel_in_background(pid: str):
    try:
        with project_guard(pid):
            while True:
                state = load_project(pid)
                if state.get("status") != "active":
                    break
                if word_count(state["manuscript"]) >= state["target_words"]:
                    state["status"] = "complete"
                    save_project(pid, state)
                    send_email(state["email"], f"'{state['title']}' is complete",
                               f"Your manuscript reached {word_count(state['manuscript'])} words. "
                               f"All chapters have been generated and emailed to you, but nothing was "
                               f"auto-published — review them on the project page and publish whichever "
                               f"ones you're happy with, chapter by chapter or all at once.")
                    break

                try:
                    write_and_publish_one_chapter(pid, state, auto_publish=False)
                except GenerationError as e:
                    print(f"[{pid}] finish-all stopped early: {e}")
                    send_alert_email(
                        state["email"],
                        f"'{state['title']}' — finish-all stopped early",
                        f"Chapter generation failed with this error and the run was stopped:\n\n{e}\n\n"
                        f"Chapters written so far are saved and emailed. You can resume with "
                        f"'Finish the whole novel now' once you're ready."
                    )
                    break
                time.sleep(8)  # stay comfortably under free-tier rate limits between chapters
    except ProjectBusy:
        print(f"[{pid}] finish-all not started — a job is already running for this project")


# ---------- routes ----------

UPLOAD_FORM = """
<!doctype html><html><head><title>Novel Writer</title>
<style>body{font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 20px}
textarea{width:100%;height:200px}input,textarea{margin-bottom:12px}
label{display:block;font-weight:bold;margin-top:16px}</style></head>
<body>
<h1>Novel Writer</h1>
<form method="POST" action="/projects" enctype="multipart/form-data">
  <label>Title</label>
  <input name="title" required style="width:100%">

  <label>Your email (chapters get sent here)</label>
  <input name="email" type="email" required style="width:100%">

  <label>Target total word count</label>
  <input name="target_words" type="number" required value="80000">

  <label>Unfinished manuscript (paste text, or upload a .txt file below)</label>
  <textarea name="manuscript_text" placeholder="Paste what you've written so far..."></textarea>
  <input type="file" name="manuscript_file" accept=".txt,.md">

  <label>Novel bible / direction (optional — where the story should go)</label>
  <textarea name="bible" placeholder="Characters, plot direction, chapter outline..."></textarea>

  <button type="submit">Start</button>
</form>
<h2>Existing projects</h2>
<ul>{% for p in projects %}<li><a href="/projects/{{p}}">{{p}}</a>
<a href="/projects/{{p}}/delete" style="color:#b00;margin-left:10px;font-size:0.9em">delete</a></li>{% endfor %}</ul>
</body></html>
"""

STATUS_PAGE = """
<!doctype html><html><head><title>{{state.title}}</title>
{% if is_finishing %}<meta http-equiv="refresh" content="10">{% endif %}
<style>
body{font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 20px}
button{padding:8px 14px;cursor:pointer;margin-right:8px}
textarea{width:100%;height:300px;font-family:monospace}
.copied{color:green;font-weight:bold;margin-left:10px}
.finishing{color:#b35a00;font-weight:bold}
</style>
<script>
function copyChapter() {
  const el = document.getElementById('chapter-text');
  el.select();
  document.execCommand('copy');
  document.getElementById('copy-status').innerText = 'Copied!';
}
</script>
</head>
<body style="font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 20px">
<h1>{{state.title}}</h1>
<p>Status: <b>{{state.status}}</b>{% if is_finishing %} <span class="finishing">— generating remaining chapters now, this page refreshes automatically</span>{% endif %}</p>
<p>{{words}} / {{state.target_words}} words &middot; {{state.chapters_written}} chapters written</p>
<p>Emailing: {{state.email}}</p>
{% if state.status == "active" and not is_finishing %}
<button onclick="location.href='/projects/{{pid}}/run-now'">Generate next chapter now</button>
<button onclick="location.href='/projects/{{pid}}/finish-all'">Finish the whole novel now</button>
{% endif %}
{% if latest_chapter %}
<h2>Latest chapter (Ch. {{state.chapters_written}})</h2>
<textarea id="chapter-text" readonly>{{latest_chapter}}</textarea>
<button onclick="copyChapter()">Copy chapter to clipboard</button>
<span id="copy-status" class="copied"></span>
<p style="color:#666;font-size:0.9em">Paste this straight into Webnovel's or NovelUp's chapter editor.</p>
{% endif %}
{% if unpublished_count > 0 %}
<p style="background:#fff3e0;padding:10px;border-radius:6px">
{{unpublished_count}} chapter(s) written but not yet published.
<button onclick="location.href='/projects/{{pid}}/publish-all'">Publish all of them now</button>
</p>
{% endif %}
{% if chapter_list %}
<h2>All chapters</h2>
<ul style="list-style:none;padding:0">
{% for c in chapter_list %}
<li style="padding:6px 0;border-bottom:1px solid #eee">
Chapter {{c.num}} — {% if c.published %}<span style="color:green">published</span>{% else %}<span style="color:#b35a00">draft</span>{% endif %}
<button onclick="location.href='/projects/{{pid}}/chapter/{{c.num}}'" style="padding:4px 10px;font-size:0.85em">View &amp; copy</button>
{% if not c.published %}<button onclick="location.href='/projects/{{pid}}/publish/{{c.num}}'" style="padding:4px 10px;font-size:0.85em">Publish this one</button>{% endif %}
</li>
{% endfor %}
</ul>
{% endif %}
<p><a href="/">&larr; back</a></p>
</body></html>
"""

CHAPTER_VIEW_PAGE = """
<!doctype html><html><head><title>{{state.title}} — Chapter {{chap_num}}</title>
<style>
body{font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 20px}
button{padding:8px 14px;cursor:pointer;margin-right:8px}
textarea{width:100%;height:400px;font-family:monospace}
.copied{color:green;font-weight:bold;margin-left:10px}
</style>
<script>
function copyChapter() {
  const el = document.getElementById('chapter-text');
  el.select();
  document.execCommand('copy');
  document.getElementById('copy-status').innerText = 'Copied!';
}
</script>
</head>
<body>
<h1>{{state.title}} — Chapter {{chap_num}}</h1>
<textarea id="chapter-text" readonly>{{chapter_text}}</textarea>
<button onclick="copyChapter()">Copy chapter to clipboard</button>
<span id="copy-status" class="copied"></span>
<p style="color:#666;font-size:0.9em">Paste this straight into Webnovel's or NovelUp's chapter editor.</p>
<p><a href="/projects/{{pid}}">&larr; back to project</a></p>
</body></html>
"""


@app.route("/")
@requires_auth
def index():
    return render_template_string(UPLOAD_FORM, projects=all_projects())


@app.route("/projects", methods=["POST"])
@requires_auth
def create_project():
    title = request.form["title"]
    email = request.form["email"]
    target_words = int(request.form["target_words"])
    bible = request.form.get("bible", "")

    manuscript_text = request.form.get("manuscript_text", "").strip()
    uploaded = request.files.get("manuscript_file")
    if uploaded and uploaded.filename:
        manuscript_text = uploaded.read().decode("utf-8", errors="ignore")

    if not manuscript_text:
        return "You need to provide manuscript text (paste or upload).", 400

    pid = str(uuid.uuid4())[:8]
    project_path(pid).mkdir(parents=True, exist_ok=True)

    try:
        style_guide = extract_style_guide(manuscript_text)
    except GenerationError as e:
        return f"Couldn't analyze the manuscript's style right now: {e}. Please try again in a minute.", 502

    state = {
        "title": title,
        "email": email,
        "target_words": target_words,
        "bible": bible,
        "manuscript": manuscript_text,
        "manuscript_tail": manuscript_text[-6000:],
        "style_guide": style_guide,
        "running_summary": "",
        "chapters_written": 0,
        "published_chapters": [],
        "status": "active",
    }
    save_project(pid, state)
    return redirect(url_for("project_status", pid=pid))


@app.route("/projects/<pid>")
@requires_auth
def project_status(pid):
    state = load_project(pid)
    latest_chapter = None
    if state.get("chapters_written", 0) > 0:
        chap_num = state["chapters_written"]
        chap_file = project_path(pid) / f"chapter_{chap_num:03d}.txt"
        if chap_file.exists():
            latest_chapter = chap_file.read_text(encoding="utf-8")

    published = set(state.get("published_chapters", []))
    total_written = state.get("chapters_written", 0)
    chapter_list = [{"num": n, "published": n in published} for n in range(1, total_written + 1)]
    unpublished_count = total_written - len(published)

    return render_template_string(STATUS_PAGE, state=state, words=word_count(state["manuscript"]),
                                   latest_chapter=latest_chapter, is_finishing=is_busy(pid), pid=pid,
                                   chapter_list=chapter_list, unpublished_count=unpublished_count)


@app.route("/projects/<pid>/chapter/<int:chap_num>")
@requires_auth
def view_chapter(pid, chap_num):
    """Shows a single chapter's full text with a copy button — works for any
    chapter, not just the latest, so you can review/copy each one into
    Webnovel or NovelUp regardless of publish status."""
    state = load_project(pid)
    chap_file = project_path(pid) / f"chapter_{chap_num:03d}.txt"
    if not chap_file.exists():
        return f"Chapter {chap_num} not found for this project.", 404
    chapter_text = chap_file.read_text(encoding="utf-8")
    return render_template_string(CHAPTER_VIEW_PAGE, state=state, chapter_text=chapter_text,
                                   chap_num=chap_num, pid=pid)


@app.route("/projects/<pid>/run-now")
@requires_auth
def run_now(pid):
    """Manual trigger: generate just the next chapter, right now."""
    state = load_project(pid)
    if state["status"] == "active" and word_count(state["manuscript"]) < state["target_words"]:
        try:
            with project_guard(pid):
                write_and_publish_one_chapter(pid, state)
        except ProjectBusy:
            pass  # a job is already running for this project — just show current status
        except GenerationError:
            pass  # error already logged; state on disk is unchanged, page will just show no new chapter
    return redirect(url_for("project_status", pid=pid))


@app.route("/projects/<pid>/finish-all")
@requires_auth
def finish_all(pid):
    """Kick off a background job that keeps generating chapters back-to-back
    until the manuscript hits its target word count — no waiting for daily runs.
    Chapters are written and emailed but NOT auto-published; review and publish
    them afterward from the project page."""
    state = load_project(pid)
    if state["status"] == "active" and not is_busy(pid):
        thread = threading.Thread(target=finish_novel_in_background, args=(pid,), daemon=True)
        thread.start()
    return redirect(url_for("project_status", pid=pid))


@app.route("/projects/<pid>/publish/<int:chap_num>")
@requires_auth
def publish_one(pid, chap_num):
    """Publish a single already-written chapter to WordPress/Facebook."""
    state = load_project(pid)
    publish_chapter_now(pid, state, chap_num)
    return redirect(url_for("project_status", pid=pid))


def _publish_all_unpublished_in_background(pid: str):
    state = load_project(pid)
    unpublished = [n for n in range(1, state.get("chapters_written", 0) + 1)
                   if n not in state.get("published_chapters", [])]
    for chap_num in unpublished:
        state = load_project(pid)  # reload in case something else changed it
        publish_chapter_now(pid, state, chap_num)
        time.sleep(3)  # small gap between publish calls


@app.route("/projects/<pid>/publish-all")
@requires_auth
def publish_all(pid):
    """Publish every chapter that's been written but not yet published, in order."""
    thread = threading.Thread(target=_publish_all_unpublished_in_background, args=(pid,), daemon=True)
    thread.start()
    return redirect(url_for("project_status", pid=pid))


@app.route("/projects/<pid>/delete", methods=["GET", "POST"])
@requires_auth
def delete_project(pid):
    """Permanently removes a project's folder (manuscript, chapters, state)
    from disk. Requires a confirmation tap — GET shows a warning page,
    POST actually deletes. Does nothing to already-published chapters on
    WordPress/Facebook, since those live on those platforms, not here."""
    path = project_path(pid)
    if not path.exists():
        return redirect(url_for("index"))

    if request.method == "POST":
        if is_busy(pid):
            return "This project has a job running right now — wait for it to finish before deleting.", 409
        shutil.rmtree(path)
        return redirect(url_for("index"))

    try:
        state = load_project(pid)
        title = state.get("title", pid)
    except Exception:  # noqa: BLE001 - even a corrupted project should still be deletable
        title = pid

    return f"""
    <!doctype html><html><body style="font-family:sans-serif;max-width:480px;margin:40px auto;padding:0 20px">
    <h2>Delete "{title}"?</h2>
    <p>This permanently deletes the project's saved manuscript, chapters, and
    settings from the server. This cannot be undone. Already-published
    chapters on WordPress/Facebook are not affected.</p>
    <form method="POST">
      <button type="submit" style="padding:8px 16px">Yes, delete permanently</button>
      <a href="/projects/{pid}" style="margin-left:12px">Cancel</a>
    </form>
    </body></html>
    """


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
