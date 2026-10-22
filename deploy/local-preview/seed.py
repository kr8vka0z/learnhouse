#!/usr/bin/env python3
"""Seeds a fresh local preview stack with a sample course and three students.

Everything goes through the public HTTP API with the request shapes the web
app sends, so the data is what real clicks would produce. Students take their
quizzes and essays through the same endpoints the student pages call; nothing
is written to the database directly.

Needs: python3 (stdlib only), curl, and docker for a throwaway ffmpeg
container that makes the test audio/video. Reads ./.env (see make-env.sh).
The org and admin already exist: the app creates them on first boot from the
LEARNHOUSE_INITIAL_* values in .env.

Usage:  ./seed.py            (DOCKER="sudo docker" ./seed.py if docker needs sudo)
Also white-labels the org (brand color, no "Powered by" watermark, logo files
from PREVIEW_LOGO_FILE / PREVIEW_SQUARE_LOGO_FILE / PREVIEW_FAVICON_FILE when
set). Re-running on a seeded stack re-applies the branding and prints the
summary; reset with `down -v`.
"""

import json
import mimetypes
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid

HERE = pathlib.Path(__file__).resolve().parent
ENV = dict(
    line.split("=", 1)
    for line in (HERE / ".env").read_text().splitlines()
    if line and not line.startswith("#") and "=" in line
)
PORT = ENV.get("HTTP_PORT", "3300")
BASE = f"http://localhost:{PORT}"
API = f"{BASE}/api/v1"
ORG_SLUG = ENV["LEARNHOUSE_INITIAL_ORG_SLUG"]
ADMIN_EMAIL = ENV["LEARNHOUSE_INITIAL_ADMIN_EMAIL"]
ADMIN_PASSWORD = ENV["LEARNHOUSE_INITIAL_ADMIN_PASSWORD"]
STUDENT_PASSWORD = ENV["PREVIEW_STUDENT_PASSWORD"]
DOCKER = shlex.split(os.environ.get("DOCKER", "docker"))


def setting(name, default=""):
    """A preview setting: the process environment first, then .env."""
    return os.environ.get(name) or ENV.get(name) or default


# Org branding, applied through the same org-settings endpoints the dashboard
# uses. Image paths are optional; each one is uploaded only when set.
LOGO_FILE = setting("PREVIEW_LOGO_FILE")                # wide logo, top menu bar
SQUARE_LOGO_FILE = setting("PREVIEW_SQUARE_LOGO_FILE")  # sign-in panel, dashboard
FAVICON_FILE = setting("PREVIEW_FAVICON_FILE")
BRAND_COLOR = setting("PREVIEW_BRAND_COLOR", "#0B1F5C")
FFMPEG_IMAGE = "jrottenberg/ffmpeg:6.1-alpine"

COURSE_NAME = "Nursing Assistant Theory — Sample"
PASS_MARK = 70
# The field is "Maximum attempts" in the dashboard: initial submission plus
# retries. Two retries after the first try = 3 attempts.
MAX_ATTEMPTS = 3

STUDENTS = {
    "fresh": ("Fresh", "Student", "fresh.student@example.com"),
    "progress": ("Progress", "Student", "progress.student@example.com"),
    "stuck": ("Stuck", "Student", "stuck.student@example.com"),
}

# (chapter name, [(question, options, index of the right option)]). Four
# questions each, so 2 right = 50% (fail) and 3 right = 75% (pass).
CHAPTERS = [
    ("Chapter 1: Infection Control", [
        ("How long should you scrub your hands?", ["5 seconds", "20 seconds", "2 minutes"], 1),
        ("Gloves replace hand hygiene.", ["True", "False"], 1),
        ("Used needles go in a...", ["Trash bag", "Sharps container", "Linen hamper"], 1),
        ("PPE stands for...", ["Personal protective equipment", "Patient privacy entry", "Post-procedure exam"], 0),
    ]),
    ("Chapter 2: Vital Signs", [
        ("Normal adult resting pulse?", ["30-50", "60-100", "120-160"], 1),
        ("Normal oral temperature is about...", ["37 C", "33 C", "40 C"], 0),
        ("Respirations are counted for...", ["10 seconds", "30-60 seconds", "5 minutes"], 1),
        ("Blood pressure is written as...", ["Systolic over diastolic", "Diastolic over systolic", "Pulse over rate"], 0),
    ]),
    ("Chapter 3: Patient Comfort", [
        ("Reposition a bed-bound patient at least every...", ["2 hours", "8 hours", "24 hours"], 0),
        ("Before helping a patient, first...", ["Explain what you will do", "Start right away", "Ask a visitor"], 0),
        ("A call light should be...", ["Within the patient's reach", "On the wall", "Turned off"], 0),
        ("Pain is reported by...", ["The patient", "The visitor", "Nobody"], 0),
    ]),
]

ESSAY = {
    "kind": "essay",
    "prompt": "Describe how you would take and record a patient's vital signs, "
              "and what you would do if one reading was outside the normal range.",
    "min_words": 50,
    "max_words": 400,
    "explanation": "A good answer explains the patient first, takes pulse, respirations, "
                   "temperature and blood pressure with the right equipment, records each "
                   "reading straight away, and reports any abnormal reading to the nurse "
                   "without delay.",
}
ESSAY_ANSWER = (
    "First I wash my hands, introduce myself and explain to the patient what I am going to do. "
    "I count the radial pulse for a full minute, then count respirations while still holding "
    "the wrist so the patient breathes normally. I take an oral temperature with a clean probe "
    "cover and measure blood pressure with a cuff of the right size. I write each reading in the "
    "chart straight away with the time. If a reading is outside the normal range I check it again, "
    "tell the nurse at once, and note in the chart who I told and when."
)


# --- HTTP ---------------------------------------------------------------------

def call(method, path, token=None, json_body=None, form=None, ok=(200, 201), quiet=False):
    """One API request through curl. Returns (status, parsed body)."""
    cmd = ["curl", "-s", "-w", "\n%{http_code}", "-X", method, API + path]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if json_body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(json_body)]
    for k, v in (form or {}).items():
        # --form-string for plain values so a leading "@" or "<" is never a file.
        cmd += ["-F" if str(v).startswith("@") else "--form-string", f"{k}={v}"]
    for _ in range(10):
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        body, code = out.rsplit("\n", 1)
        status = int(code)
        # 502 = the app's nginx found no live API process (it marks the API
        # down for 10 s after one refused connection, which happens while the
        # API is still starting). The request never reached the API, so
        # sending it again is safe.
        if status != 502:
            break
        time.sleep(3)
    try:
        data = json.loads(body) if body.strip() else None
    except json.JSONDecodeError:
        data = body
    if ok and status not in ok:
        if quiet:
            return status, data
        sys.exit(f"{method} {path} -> {status}: {str(body)[:800]}")
    return status, data


def login(email, password):
    _, data = call("POST", "/auth/login", form={"username": email, "password": password})
    return data["tokens"]["access_token"], data["user"]


# --- Test media -----------------------------------------------------------------

def make_pdf(path, pages):
    """Minimal multi-page text PDF, no dependencies."""
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    font = 3 + 2 * n
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", f"<< /Type /Pages /Kids [{kids}] /Count {n} >>"]
    for i, text in enumerate(pages):
        stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET"
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {4 + 2 * i} 0 R >>")
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    data, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(data))
        data += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(data)
    data += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    data += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    data += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(data)


def ffmpeg(outdir, *args):
    # There is no ffmpeg on the host; a throwaway container makes the files.
    # -u keeps the output owned by the caller instead of root.
    subprocess.run(
        [*DOCKER, "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{outdir}:/out",
         FFMPEG_IMAGE, "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
    )


def make_media(d):
    for i, (name, _) in enumerate(CHAPTERS, 1):
        make_pdf(d / f"ch{i}-reading.pdf", [f"{name}", "Sample reading, page 2", "Sample reading, page 3"])
        make_pdf(d / f"ch{i}-slides.pdf", [f"{name}: slide {s}" for s in range(1, 4)])
        # 4-second test pattern with a tone: short enough to play to the end.
        ffmpeg(d, "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=4",
               "-f", "lavfi", "-i", f"sine=frequency={440 + 110 * i}:duration=4",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
               "-movflags", "+faststart", f"/out/ch{i}-video.mp4")
        ffmpeg(d, "-f", "lavfi", "-i", f"sine=frequency={330 + 55 * i}:duration=6",
               "-c:a", "libmp3lame", "-b:a", "64k", f"/out/ch{i}-narration.mp3")


# --- Course building --------------------------------------------------------------

def quiz_questions(spec):
    return [{
        "questionText": text,
        "questionUUID": f"question_{uuid.uuid4()}",
        "response_type": "single",
        "options": [{"optionUUID": f"option_{uuid.uuid4()}", "text": o, "fileID": "",
                     "type": "text", "assigned_right_answer": i == right}
                    for i, o in enumerate(opts)],
    } for text, opts, right in spec]


def new_assignment(tok, org_id, course_id, chapter_id, title, description, auto_grading):
    _, act = call("POST", f"/activities/?coursechapter_id={chapter_id}&org_id={org_id}", tok, {
        "name": title, "chapter_id": chapter_id, "activity_type": "TYPE_ASSIGNMENT",
        "activity_sub_type": "SUBTYPE_ASSIGNMENT_ANY", "published": False,
        "course_id": course_id, "content": {}})
    _, asg = call("POST", "/assignments/", tok, {
        "title": title, "description": description, "due_date": None,
        "grading_type": "PERCENTAGE", "ungraded": False, "auto_grading": auto_grading,
        "anti_copy_paste": False, "show_correct_answers": False, "allow_retries": True,
        "max_retries": MAX_ATTEMPTS, "pass_threshold_percentage": PASS_MARK,
        "course_id": course_id, "org_id": org_id, "chapter_id": chapter_id,
        "activity_id": act["id"]})
    return act, asg


def publish_assignment(tok, act, asg):
    call("PUT", f"/assignments/{asg['assignment_uuid']}", tok, {"published": True})
    call("PUT", f"/activities/{act['activity_uuid']}", tok, {"published": True})


def build_course(tok, org_id, media):
    _, course = call("POST", f"/courses/?org_id={org_id}", tok, form={
        "name": COURSE_NAME, "description": "Sample course for the local preview",
        "public": "true", "learnings": "", "tags": "", "about": "Sample course for the local preview"})
    cid, cuuid = course["id"], course["course_uuid"]
    # Same write as the dashboard's "Sequential chapters" toggle: extra_metadata
    # is replaced wholesale, so send the existing object with the key set.
    _, full = call("GET", f"/courses/{cuuid}", tok)
    extra = {**(full.get("extra_metadata") or {}), "rr_sequential_chapters": True}
    call("PUT", f"/courses/{cuuid}", tok, {"published": True, "public": True, "extra_metadata": extra})

    chapters = []
    for i, (name, spec) in enumerate(CHAPTERS, 1):
        _, ch = call("POST", "/chapters/", tok, {"name": name, "description": "", "thumbnail_image": "",
                                                "org_id": org_id, "course_id": cid})
        chid = ch["id"]
        acts = {}

        _, a = call("POST", "/activities/documentpdf", tok, form={
            "chapter_id": chid, "name": "Reading",
            "pdf_file": f"@{media / f'ch{i}-reading.pdf'};type=application/pdf"})
        acts["pdf"] = a

        # Narrated slides: a page with a PDF block and an audio block.
        _, dyn = call("POST", f"/activities/?coursechapter_id={chid}&org_id={org_id}", tok, {
            "name": "Narrated slides", "chapter_id": chid, "activity_type": "TYPE_DYNAMIC",
            "activity_sub_type": "SUBTYPE_DYNAMIC_PAGE", "published": True, "course_id": cid, "content": {}})
        _, pdfb = call("POST", "/blocks/pdf", tok, form={
            "activity_uuid": dyn["activity_uuid"],
            "file_object": f"@{media / f'ch{i}-slides.pdf'};type=application/pdf"})
        _, audb = call("POST", "/blocks/audio", tok, form={
            "activity_uuid": dyn["activity_uuid"],
            "file_object": f"@{media / f'ch{i}-narration.mp3'};type=audio/mpeg"})
        call("PUT", f"/activities/{dyn['activity_uuid']}", tok, {"content": {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Listen to the narration while reading the slides."}]},
            {"type": "blockPDF", "attrs": {"blockObject": pdfb}},
            # The editor's upload path stores source_type/size; without
            # source_type the audio block renders nothing.
            {"type": "blockAudio", "attrs": {"blockObject": {**audb, "source_type": "upload", "size": "medium"}}},
        ]}})
        acts["narrated"] = dyn

        _, a = call("POST", "/activities/video", tok, form={
            "chapter_id": chid, "name": "Video",
            "video_file": f"@{media / f'ch{i}-video.mp4'};type=video/mp4"})
        acts["video"] = a

        # PDF and video activities are created unpublished.
        for key in ("pdf", "video"):
            call("PUT", f"/activities/{acts[key]['activity_uuid']}", tok, {"published": True})

        questions = quiz_questions(spec)
        act, asg = new_assignment(tok, org_id, cid, chid, f"Chapter {i} quiz",
                                  f"Pass mark {PASS_MARK}%. Up to {MAX_ATTEMPTS} attempts.", True)
        _, task = call("POST", f"/assignments/{asg['assignment_uuid']}/tasks", tok, {
            "title": "Knowledge check", "description": "", "hint": "", "reference_file": "",
            "assignment_type": "QUIZ", "contents": {}, "max_grade_value": 100})
        call("PUT", f"/assignments/{asg['assignment_uuid']}/tasks/{task['assignment_task_uuid']}", tok,
             {"contents": {"questions": questions, "grading_mode": "all_or_nothing"}})
        publish_assignment(tok, act, asg)
        quiz = {"activity": act, "assignment_uuid": asg["assignment_uuid"],
                "task_uuid": task["assignment_task_uuid"], "questions": questions}

        essay = None
        if i == 2:
            act, asg = new_assignment(tok, org_id, cid, chid, "Chapter 2 essay",
                                      "Written answer, graded by your instructor.", False)
            _, task = call("POST", f"/assignments/{asg['assignment_uuid']}/tasks", tok, {
                "title": "Taking vital signs", "description": "", "hint": "", "reference_file": "",
                "assignment_type": "CUSTOM", "contents": ESSAY, "max_grade_value": 100})
            # Task updates replace contents wholesale; set it explicitly like the editor does.
            call("PUT", f"/assignments/{asg['assignment_uuid']}/tasks/{task['assignment_task_uuid']}", tok,
                 {"contents": ESSAY})
            publish_assignment(tok, act, asg)
            essay = {"activity": act, "assignment_uuid": asg["assignment_uuid"],
                     "task_uuid": task["assignment_task_uuid"]}

        chapters.append({"name": name, "acts": acts, "quiz": quiz, "essay": essay})
    return cuuid, chapters


# --- Student actions ------------------------------------------------------------

def mark_done(tok, activity):
    call("POST", f"/trail/add_activity/{activity['activity_uuid']}", tok)


def take_quiz(tok, quiz, n_right):
    """Answer the first n_right questions correctly and the rest wrongly, hand in,
    and return the graded submission."""
    subs = []
    for qi, q in enumerate(quiz["questions"]):
        right = next(i for i, o in enumerate(q["options"]) if o["assigned_right_answer"])
        pick = right if qi < n_right else (right + 1) % len(q["options"])
        subs += [{"questionUUID": q["questionUUID"], "optionUUID": o["optionUUID"], "answer": i == pick}
                 for i, o in enumerate(q["options"])]
    auuid = quiz["assignment_uuid"]
    call("PUT", f"/assignments/{auuid}/tasks/{quiz['task_uuid']}/submissions", tok, {
        "assignment_task_submission_uuid": None,
        "task_submission": {"questions": [], "submissions": subs},
        "grade": 0, "task_submission_grade_feedback": ""})
    call("POST", f"/assignments/{auuid}/submissions", tok)
    return my_submission(tok, auuid)


def my_submission(tok, auuid):
    _, data = call("GET", f"/assignments/{auuid}/submissions/me", tok)
    return data[0] if isinstance(data, list) else data


def retry(tok, quiz, ok=(200, 201)):
    return call("POST", f"/assignments/{quiz['assignment_uuid']}/submissions/me/retry", tok,
                ok=ok, quiet=ok != (200, 201))


def expect(cond, what):
    if not cond:
        sys.exit(f"Seed check failed: {what}")


def quiz_attempt(tok, quiz, n_right, should_pass):
    sub = take_quiz(tok, quiz, n_right)
    grade = int(sub.get("grade") or 0)
    expect(sub.get("submission_status") == "GRADED", f"quiz not auto-graded: {sub}")
    expect((grade >= PASS_MARK) == should_pass, f"expected {'pass' if should_pass else 'fail'}, got {grade}%")
    return sub


# --- Org branding -------------------------------------------------------------------

def upload_image(tok, org_id, endpoint, field, path):
    p = pathlib.Path(path).expanduser()
    if not p.is_file():
        sys.exit(f"{field}: no such file {p}")
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    call("PUT", f"/orgs/{org_id}/{endpoint}", tok, form={field: f"@{p};type={mime}"})


def brand_org(tok, org):
    """White-label the org: brand color, no "Powered by" watermark (site and
    email), mail sender name = org name, AI and unused features off (only
    Courses stays in the menu), and the logo files
    when given.
    Idempotent, so it also runs on an already-seeded stack."""
    org_id = org["id"]
    q = urllib.parse.quote
    call("PUT", f"/orgs/{org_id}/config/color?color={q(BRAND_COLOR)}", tok)
    call("PUT", f"/orgs/{org_id}/config/watermark?watermark_enabled=false", tok)
    call("PUT", f"/orgs/{org_id}/config/email_sender_name?email_sender_name={q(org['name'])}", tok)
    # No AI key in a preview: turn the org's AI features off (same switch as
    # Settings > AI), which also removes the "Ask AI" button for students.
    call("PUT", f"/orgs/{org_id}/config/ai?ai_enabled=false&copilot_enabled=false", tok)
    # Features the school doesn't use, off through the same per-feature
    # switches as the dashboard, so the student menu shows only Courses.
    # "folders" is the Library.
    for feature in ("communities", "folders", "podcasts", "boards", "playgrounds"):
        call("PUT", f"/orgs/{org_id}/config/{feature}?{feature}_enabled=false", tok)
    for path, endpoint, field in ((LOGO_FILE, "logo", "logo_file"),
                                  (SQUARE_LOGO_FILE, "square_logo", "square_logo_file"),
                                  # The thumbnail is the org's link-preview image.
                                  (SQUARE_LOGO_FILE, "thumbnail", "thumbnail_file"),
                                  (FAVICON_FILE, "favicon", "favicon_file")):
        if path:
            upload_image(tok, org_id, endpoint, field, path)


# --- Main -------------------------------------------------------------------------

def find_course(tok):
    _, courses = call("GET", f"/courses/org_slug/{ORG_SLUG}/page/1/limit/100", tok)
    return next((c for c in courses or [] if c.get("name") == COURSE_NAME), None)


def summary(course_uuid):
    short = course_uuid.removeprefix("course_")
    print("\nLocal preview ready")
    print(f"  URL:     {BASE}")
    print(f"  Course:  {BASE}/course/{short}")
    print(f"  Admin:   {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
    for key, (first, last, email) in STUDENTS.items():
        print(f"  {first} {last}: {email} / {STUDENT_PASSWORD}")
    print("  (passwords come from .env)")


def main():
    admin_tok, _ = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    _, org = call("GET", f"/orgs/slug/{ORG_SLUG}", admin_tok)
    org_id = org["id"]
    print("Branding the organization...")
    brand_org(admin_tok, org)

    existing = find_course(admin_tok)
    if existing:
        # Stuck Student is the last thing seeded; if they can't log in, an
        # earlier run stopped partway and the data is incomplete.
        status, _ = call("POST", "/auth/login", ok=None,
                         form={"username": STUDENTS["stuck"][2], "password": STUDENT_PASSWORD})
        if status != 200:
            sys.exit("A previous seed run stopped partway. Reset with "
                     "`docker compose down -v`, then `up -d` and seed again.")
        print("Already seeded. To start over: docker compose down -v, then up -d and seed again.")
        summary(existing["course_uuid"])
        return

    with tempfile.TemporaryDirectory(prefix="lh-preview-media-") as tmp:
        media = pathlib.Path(tmp)
        print("Making test media...")
        make_media(media)
        print("Building course...")
        cuuid, chapters = build_course(admin_tok, org_id, media)

    print("Creating students...")
    tokens = {}
    for key, (first, last, email) in STUDENTS.items():
        status, _ = call("POST", f"/users/{org_id}", None, {
            "username": f"{first.lower()}-{last.lower()}", "first_name": first, "last_name": last,
            "email": email, "password": STUDENT_PASSWORD}, ok=None)
        expect(status in (200, 201, 400, 409), f"creating {email} returned {status}")
        tokens[key], _ = login(email, STUDENT_PASSWORD)
        # Reading the trail creates it on first visit (the web app does this
        # on page load); enrolling fails with "Trail not found" without it.
        call("GET", f"/trail/org/{org_id}/trail", tokens[key])
        call("POST", f"/trail/add_course/{cuuid}", tokens[key])

    ch1, ch2, _ = chapters

    print("Progress Student: chapter 1, then most of chapter 2...")
    tok = tokens["progress"]
    for key in ("pdf", "narrated", "video"):
        mark_done(tok, ch1["acts"][key])
    quiz_attempt(tok, ch1["quiz"], 2, should_pass=False)
    retry(tok, ch1["quiz"])
    quiz_attempt(tok, ch1["quiz"], 4, should_pass=True)
    # Sequential gating: every chapter 2 activity, including the narrated
    # page, must be done for chapter 3 to open.
    for key in ("pdf", "narrated", "video"):
        mark_done(tok, ch2["acts"][key])
    quiz_attempt(tok, ch2["quiz"], 3, should_pass=True)
    essay = ch2["essay"]
    call("PUT", f"/assignments/{essay['assignment_uuid']}/tasks/{essay['task_uuid']}/submissions", tok, {
        "assignment_task_submission_uuid": None, "task_submission": {"answer": ESSAY_ANSWER},
        "grade": 0, "task_submission_grade_feedback": ""})
    call("POST", f"/assignments/{essay['assignment_uuid']}/submissions", tok)
    sub = my_submission(tok, essay["assignment_uuid"])
    expect(sub.get("submission_status") == "SUBMITTED", f"essay not awaiting grading: {sub}")

    print("Stuck Student: chapter 1 quiz failed on every attempt...")
    tok = tokens["stuck"]
    for key in ("pdf", "narrated", "video"):
        mark_done(tok, ch1["acts"][key])
    for attempt in range(1, MAX_ATTEMPTS + 1):
        quiz_attempt(tok, ch1["quiz"], 1, should_pass=False)
        if attempt < MAX_ATTEMPTS:
            retry(tok, ch1["quiz"])
    status, _ = retry(tok, ch1["quiz"], ok=(403,))
    expect(status == 403, f"a retry past the limit returned {status}, expected 403")

    summary(cuuid)


if __name__ == "__main__":
    main()
