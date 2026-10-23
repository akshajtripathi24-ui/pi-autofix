"""Pi AutoFix - an AI coding helper that tests its own code and keeps fixing it until the tests pass.

The AI model runs on another computer with Ollama (free, local). This Pi runs the website
and tests every piece of code in a locked-down sandbox before showing it to you.

Run in the background:  bash setup-autostart.sh   (see README.md)
Run by hand:            python3 server.py
Then open http://<your-pi-ip>:8001
"""
import json, os, re, secrets, shutil, subprocess, tempfile, threading, time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest, error as urlerror
from urllib.parse import urlparse

PORT = 8001
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
JOBS_DIR = os.path.join(HERE, "jobs")
DEFAULT_CONFIG = {"ollama_url": "", "model": "qwen2.5-coder:3b", "max_attempts": 5}

SANDBOX_USER = "autofix"      # created by setup-sandbox.sh
SANDBOX_PYTHON = "/usr/bin/python3"
TEST_TIMEOUT = 20             # seconds a test run may take (wall clock)
CPU_LIMIT = 15                # seconds of CPU time
MEMORY_LIMIT = 512 * 1024 * 1024
OUTPUT_LIMIT = 4000           # characters of test output sent back to the AI
AI_TIMEOUT = 900              # seconds to wait for one AI answer
KEEP_JOBS = 50

config = dict(DEFAULT_CONFIG)
jobs = {}                     # id -> job dict
jobs_lock = threading.RLock()
work_lock = threading.Lock()  # one job at a time: the AI can only think about one thing
sandbox = {"user": False, "network_blocked": False, "checked": False}


# ---------- helpers ----------

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def load_config():
    config.update({k: v for k, v in load_json(CONFIG_FILE, {}).items() if k in DEFAULT_CONFIG})


# ---------- the AI (Ollama on another computer) ----------

def ollama(path, body=None, timeout=10):
    url = config["ollama_url"].rstrip("/")
    if not url:
        raise RuntimeError("Set your laptop's Ollama address in ⚙️ Settings first.")
    if not url.startswith("http"):
        url = "http://" + url
    data = json.dumps(body).encode() if body is not None else None
    req = urlrequest.Request(url + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlrequest.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urlerror.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code == 404 and "model" in detail:
            raise RuntimeError(f"The laptop doesn't have the model {config['model']}. "
                               f"On the laptop run: ollama pull {config['model']}")
        raise RuntimeError(f"Ollama error {e.code}: {detail}")
    except (urlerror.URLError, OSError) as e:
        raise RuntimeError("Can't reach the AI on your laptop. Is the laptop on, awake, on the same Wi-Fi, "
                           f"and is Ollama running? ({getattr(e, 'reason', e)})")


def ask_ai(system, user):
    reply = ollama("/api/chat", {
        "model": config["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "keep_alive": "30m",                      # keep the model loaded between attempts
        "options": {"temperature": 0.2, "num_ctx": 8192},
    }, timeout=AI_TIMEOUT)
    return reply.get("message", {}).get("content", "")


SYSTEM_PROMPT = """You are an expert Python programmer. You write correct code AND unit tests for it.

Always reply with exactly these two files, in this exact format:

### solution.py
```python
(the complete code)
```

### test_solution.py
```python
(the complete tests)
```

Then write a short explanation (2-4 sentences) for a beginner.

Rules for solution.py:
- Complete, working code. Put the logic in functions or classes.
- Never call input() and never run anything when the file is imported.
  Put any demo inside: if __name__ == "__main__":
- Only use the Python standard library. No internet. No reading or writing files outside the current folder.

Rules for test_solution.py:
- Start with: import unittest
  and: from solution import *
- At least 4 test methods (def test_...) in a unittest.TestCase class, covering normal cases AND edge cases
  (empty input, zero, negative numbers, wrong types where it makes sense).
- Check exact expected values. Make sure every expected value in the tests is actually correct.
- End with: if __name__ == "__main__": unittest.main()"""


def first_prompt(job):
    if job["mode"] == "fix":
        goal = job["task"].strip() or "Work out what the code is meant to do from its names and comments."
        return (f"Here is code from a user. It may have bugs.\n\n```python\n{job['code']}\n```\n\n"
                f"What it should do: {goal}\n\n"
                "Write solution.py: the fixed code (keep the same function and class names so it still works "
                "for the user). Write test_solution.py: tests that check it does what it should.")
    return f"Write a Python program for this request:\n\n{job['task']}"


def fix_prompt(job, attempt):
    return (f"The original request was:\n{first_prompt(job)}\n\n"
            f"Here is the current code:\n\n### solution.py\n```python\n{attempt['solution']}\n```\n\n"
            f"### test_solution.py\n```python\n{attempt['tests']}\n```\n\n"
            f"The tests FAILED. Here is the test output:\n```\n{attempt['test_output'][-OUTPUT_LIMIT:]}\n```\n\n"
            "Find the bug and fix it. Fix solution.py. Only change a test if the test itself is clearly wrong "
            "(for example it expects a wrong answer). Do not delete tests. "
            "Reply with BOTH complete files again in the same format, then a short explanation of what you fixed.")


FENCE = re.compile(r"```[a-zA-Z0-9_+-]*[ \t]*\n(.*?)```", re.S)


def parse_reply(text):
    """Pull solution.py, test_solution.py and the explanation out of the AI's answer."""
    files = {}
    for m in re.finditer(r"#+\s*`?([\w./-]+\.py)`?\s*\n+```[a-zA-Z0-9_+-]*[ \t]*\n(.*?)```", text, re.S):
        name = os.path.basename(m.group(1))
        files[name] = m.group(2)
    solution, tests = files.get("solution.py"), files.get("test_solution.py")
    if solution is None or tests is None:            # fall back: work it out from the code blocks
        blocks = FENCE.findall(text)
        test_blocks = [b for b in blocks if "unittest" in b and "def test" in b]
        other = [b for b in blocks if b not in test_blocks]
        tests = tests or (test_blocks[0] if test_blocks else None)
        solution = solution or (max(other, key=len) if other else None)
    explanation = FENCE.sub("", re.sub(r"#+\s*`?[\w./-]+\.py`?\s*\n", "", text)).strip()
    return solution, tests, explanation[-1500:]


def count_tests(tests):
    return len(re.findall(r"^\s*def test\w*\s*\(", tests or "", re.M))


# ---------- the sandbox: run the tests safely ----------

def sandbox_prefix():
    """Build the command that runs Python locked down: other user, no network, time limit."""
    cmd = []
    if sandbox["user"]:
        cmd += ["sudo", "-n", "-u", SANDBOX_USER]
    cmd += ["/usr/bin/timeout", "-s", "KILL", str(TEST_TIMEOUT)]
    if sandbox["network_blocked"]:
        cmd += ["/usr/bin/unshare", "-rn"]
    return cmd + [SANDBOX_PYTHON, "-B", "-E", "-s"]


def check_sandbox():
    def works(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, timeout=15).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    py = [SANDBOX_PYTHON, "-c", "print(1)"]
    sandbox["user"] = works(["sudo", "-n", "-u", SANDBOX_USER, "/usr/bin/timeout", "5"] + py)
    prefix = (["sudo", "-n", "-u", SANDBOX_USER] if sandbox["user"] else []) + ["/usr/bin/timeout", "5"]
    sandbox["network_blocked"] = works(prefix + ["/usr/bin/unshare", "-rn"] + py)
    sandbox["checked"] = True
    print(f"Sandbox: separate user {'ON' if sandbox['user'] else 'OFF (run setup-sandbox.sh)'}, "
          f"network blocked {'ON' if sandbox['network_blocked'] else 'OFF'}")


def limit_resources():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT, CPU_LIMIT))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))
    resource.setrlimit(resource.RLIMIT_FSIZE, (5 * 1024 * 1024, 5 * 1024 * 1024))


def run_tests(solution, tests):
    """Returns dict: passed, ran, failures, errors, output."""
    folder = tempfile.mkdtemp(prefix="autofix-")
    try:
        os.chmod(folder, 0o777)                      # the sandbox user needs to read and write here
        for name, content in (("solution.py", solution), ("test_solution.py", tests)):
            path = os.path.join(folder, name)
            with open(path, "w") as f:
                f.write(content)
            os.chmod(path, 0o644)
        cmd = sandbox_prefix() + ["-m", "unittest", "-v", "test_solution"]
        env = {"PATH": "/usr/bin:/bin", "HOME": folder, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            p = subprocess.run(cmd, cwd=folder, env=env, capture_output=True, text=True, errors="replace",
                               timeout=TEST_TIMEOUT + 10, preexec_fn=limit_resources, stdin=subprocess.DEVNULL)
            output, code = (p.stdout + p.stderr)[-20000:], p.returncode
        except subprocess.TimeoutExpired:
            output, code = "", -9
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if code in (-9, 137):
        output += f"\nTIMED OUT: the tests took longer than {TEST_TIMEOUT} seconds (an infinite loop, or waiting for input()?)"
    ran = int((re.findall(r"^Ran (\d+) tests?", output, re.M) or ["0"])[-1])
    m = re.search(r"^FAILED \((.*)\)", output, re.M)
    counts = dict(re.findall(r"(\w+)=(\d+)", m.group(1))) if m else {}
    failures, errors = int(counts.get("failures", 0)), int(counts.get("errors", 0))
    passed = code == 0 and ran > 0 and re.search(r"^OK\b", output, re.M) is not None
    if ran == 0 and code == 0:
        output += "\nNO TESTS RAN: test_solution.py must contain unittest test methods named test_..."
    return {"passed": passed, "ran": ran, "failures": failures, "errors": errors, "output": output}


# ---------- jobs: write, test, fix, repeat ----------

def save_job(job):
    with jobs_lock:
        save_json(os.path.join(JOBS_DIR, job["id"] + ".json"), job)


def run_job(job):
    with work_lock:
        job["status"] = "running"
        save_job(job)
        prev = None
        try:
            for n in range(1, int(config["max_attempts"]) + 1):
                if job.get("cancel"):
                    job["status"], job["message"] = "cancelled", "Stopped by you."
                    break
                attempt = {"n": n, "phase": "ai", "started": time.time(), "model": config["model"]}
                job["attempts"].append(attempt)
                save_job(job)

                prompt = first_prompt(job) if prev is None else fix_prompt(job, prev)
                t0 = time.time()
                reply = ask_ai(SYSTEM_PROMPT, prompt)
                attempt["ai_seconds"] = round(time.time() - t0)
                solution, tests, explanation = parse_reply(reply)
                attempt["explanation"] = explanation

                if not solution or not tests:
                    attempt.update(phase="done", passed=False, solution=solution or (prev or {}).get("solution", ""),
                                   tests=tests or (prev or {}).get("tests", ""),
                                   test_output="The AI's answer didn't contain both files (solution.py and "
                                               "test_solution.py) in the right format.",
                                   note="The AI didn't follow the answer format. Asking again.")
                    prev = attempt if attempt["solution"] and attempt["tests"] else prev
                    save_job(job)
                    continue

                # Don't let the AI "fix" things by deleting tests
                if prev is not None and count_tests(tests) < count_tests(prev["tests"]):
                    attempt["note"] = "The AI tried to remove tests, so the earlier tests were kept."
                    tests = prev["tests"]
                elif prev is not None and tests.strip() != prev["tests"].strip():
                    attempt["tests_changed"] = True
                attempt.update(solution=solution, tests=tests, phase="testing", test_count=count_tests(tests))
                save_job(job)

                result = run_tests(solution, tests)
                attempt.update(phase="done", passed=result["passed"], ran=result["ran"],
                               failures=result["failures"], errors=result["errors"], test_output=result["output"])
                save_job(job)
                prev = attempt
                if result["passed"]:
                    job["status"] = "passed"
                    job["message"] = f"All {result['ran']} tests passed on attempt {n}."
                    break
            else:
                job["status"] = "failed"
                job["message"] = (f"Still failing after {config['max_attempts']} attempts. Try describing it more "
                                  "clearly, raising the attempt limit, or switching to the smarter model in ⚙️ Settings.")
        except Exception as e:  # AI unreachable, etc.
            job["status"], job["message"] = "error", str(e)
            if job["attempts"] and job["attempts"][-1].get("phase") != "done":
                job["attempts"][-1].update(phase="done", passed=False, note=str(e))
        job["finished"] = now()
        if any(a.get("tests_changed") for a in job["attempts"]):
            job["tests_changed"] = True
        save_job(job)
        prune_jobs()


def prune_jobs():
    with jobs_lock:
        old = sorted(jobs.values(), key=lambda j: j["created"])[:-KEEP_JOBS]
        for j in old:
            jobs.pop(j["id"], None)
            try:
                os.remove(os.path.join(JOBS_DIR, j["id"] + ".json"))
            except OSError:
                pass


def load_jobs():
    os.makedirs(JOBS_DIR, exist_ok=True)
    for name in os.listdir(JOBS_DIR):
        if name.endswith(".json"):
            j = load_json(os.path.join(JOBS_DIR, name), None)
            if j and "id" in j:
                if j.get("status") in ("queued", "running"):   # the Pi restarted mid-job
                    j["status"], j["message"] = "error", "Interrupted because the tracker restarted."
                jobs[j["id"]] = j


def busy_job():
    with jobs_lock:
        return next((j["id"] for j in jobs.values() if j["status"] in ("queued", "running")), None)


def summary(j):
    s = {k: j.get(k) for k in ("id", "mode", "title", "status", "created", "message")}
    s["attempts"] = len(j.get("attempts", []))
    return s


# ---------- API ----------

def api_status():
    ai = {"ok": False, "models": [], "error": None}
    try:
        tags = ollama("/api/tags", timeout=4)
        ai["models"] = sorted(m["name"] for m in tags.get("models", []))
        ai["ok"] = True
        if config["model"] not in ai["models"]:
            ai["error"] = f"The laptop doesn't have {config['model']}. On the laptop run: ollama pull {config['model']}"
    except RuntimeError as e:
        ai["error"] = str(e)
    return {"ai": ai, "config": config, "sandbox": sandbox, "busy": busy_job()}


def api_start(body):
    mode = body.get("mode")
    task, code = str(body.get("task", ""))[:4000], str(body.get("code", ""))[:20000]
    if mode not in ("build", "fix"):
        return 400, {"error": "Unknown mode"}
    if mode == "build" and len(task.strip()) < 5:
        return 400, {"error": "Describe what you want the program to do."}
    if mode == "fix" and not code.strip():
        return 400, {"error": "Paste the code you want fixed."}
    if busy_job():
        return 409, {"error": "Already working on something. Wait for it to finish, or stop it."}
    title = (task.strip() or code.strip()).splitlines()[0][:80]
    job = {"id": datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2), "mode": mode, "task": task,
           "code": code, "title": title, "status": "queued", "created": now(), "attempts": [], "message": ""}
    with jobs_lock:
        jobs[job["id"]] = job
    save_job(job)
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return 200, {"id": job["id"]}


def api_config(body):
    url = str(body.get("ollama_url", config["ollama_url"])).strip()
    if url and not re.fullmatch(r"(https?://)?[\w.-]+(:\d{1,5})?/?", url):
        return 400, {"error": "That doesn't look like an address. Example: 192.168.1.150:11434"}
    model = str(body.get("model", config["model"])).strip()
    try:
        attempts = int(body.get("max_attempts", config["max_attempts"]))
    except (TypeError, ValueError):
        return 400, {"error": "Max attempts must be a number"}
    if not 1 <= attempts <= 10:
        return 400, {"error": "Max attempts must be between 1 and 10"}
    if url and not url.startswith("http"):
        url = "http://" + url
    if url and not re.search(r":\d+/?$", url):
        url = url.rstrip("/") + ":11434"
    config.update(ollama_url=url, model=model or DEFAULT_CONFIG["model"], max_attempts=attempts)
    save_json(CONFIG_FILE, config)
    return 200, {"ok": True, "config": config}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/":
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self.send(f.read(), "text/html; charset=utf-8")
            if path == "/api/status":
                return self.send_json(api_status())
            if path == "/api/jobs":
                with jobs_lock:
                    items = sorted(jobs.values(), key=lambda j: j["created"], reverse=True)
                    return self.send_json([summary(j) for j in items])
            m = re.fullmatch(r"/api/jobs/([\w-]+)", path)
            if m:
                with jobs_lock:
                    j = jobs.get(m.group(1))
                    data = json.loads(json.dumps(j)) if j else None
                if data:
                    data["server_now"] = time.time()     # lets the page show "thinking… 0:34" with the Pi's clock
                return self.send_json(data or {"error": "Not found"}, 200 if data else 404)
            self.send(b"Not found", "text/plain", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        path = urlparse(self.path).path
        # Only accept JSON: browsers won't let other websites send that to the Pi without asking first
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send_json({"error": "Expected JSON"}, 415)
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 100000)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, OSError):
            return self.send_json({"error": "Bad request"}, 400)
        try:
            if path == "/api/jobs":
                return self.send_json(*reversed(api_start(body)))
            if path == "/api/config":
                return self.send_json(*reversed(api_config(body)))
            m = re.fullmatch(r"/api/jobs/([\w-]+)/cancel", path)
            if m and m.group(1) in jobs:
                jobs[m.group(1)]["cancel"] = True
                return self.send_json({"ok": True, "note": "It will stop after the AI finishes its current answer."})
            self.send(b"Not found", "text/plain", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, obj, status_code=200):
        self.send(json.dumps(obj).encode(), "application/json", status_code)

    def send(self, body, ctype, status_code=200):
        self.send_response(status_code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    load_config()
    load_jobs()
    check_sandbox()
    print(f"Pi AutoFix running! Open http://<your-pi-ip>:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
