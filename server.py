"""Pi AutoFix - an AI coding helper that tests its own code and keeps fixing it until the tests pass.

The AI model runs on another computer with Ollama (free, local). This Pi runs the website
and tests every piece of code in a locked-down sandbox before showing it to you.

Run in the background:  bash setup-autostart.sh   (see README.md)
Run by hand:            python3 server.py
Then open http://<your-pi-ip>:8001
"""
import ast, json, os, re, secrets, shutil, subprocess, tempfile, threading, time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest, error as urlerror
from urllib.parse import urlparse

PORT = 8001
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
JOBS_DIR = os.path.join(HERE, "jobs")
LESSONS_FILE = os.path.join(HERE, "lessons.json")
DEFAULT_CONFIG = {"ollama_url": "", "model": "qwen2.5-coder:3b", "max_attempts": 5, "min_strength": 70,
                  "use_lessons": True}

SANDBOX_USER = "autofix"      # created by setup-sandbox.sh
SANDBOX_PYTHON = "/usr/bin/python3"
TEST_TIMEOUT = 20             # seconds a test run may take (wall clock)
DEMO_TIMEOUT = 10             # seconds the demo (if __name__ == "__main__") may take
MAX_MUTANTS = 15              # planted bugs per test-strength check
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
- Functions and methods RETURN their results. Do not print inside them (printing is only for the demo).
- For invalid input (a bad index, empty or blank text, a wrong type, a missing item), raise a clear exception
  (ValueError, IndexError, KeyError or TypeError) with a helpful message. Never just print an error and carry on.
- Make every function and method do exactly what its name says, and make each one different.
  For example, "complete" marks something as done; it does not delete it.
- Be consistent: indexes start at 0 everywhere, unless the request says otherwise.
- Never call input() and never run anything when the file is imported.
- Add a short demo inside: if __name__ == "__main__":  that uses the main features and prints the results.
  The demo WILL be run, so it must work without crashing.
- Only use the Python standard library. No internet. No reading or writing files outside the current folder.

Rules for test_solution.py:
- Start with: import unittest
  and: from solution import *
- At least 4 test methods (def test_...) in a unittest.TestCase class, covering normal cases AND edge cases
  (empty input, zero, negative numbers, wrong types where it makes sense).
- Check exact expected values. Make sure every expected value in the tests is actually correct.
- End with: if __name__ == "__main__": unittest.main()"""


def examples_text(job):
    if not job.get("examples", "").strip():
        return ""
    return ("\n\nThe user gave these examples. They are the TRUTH: the code must behave exactly like this:\n"
            + job["examples"].strip())


# ---------- 🧠 lessons: learn from the bugs the tests missed ----------

# Each kind of planted bug that slips past the tests teaches one fixed lesson.
LESSONS = {
    "and_or": ("🔀", "When a condition uses `and` / `or`, test each part on its own. For range checks like "
               "`index < 0 or index >= len(items)`, test a negative index (-1) AND a too-big index separately, "
               "and expect an error for both."),
    "boundary": ("📏", "Test the exact boundary values. For a limit like `x < 10`, test 9, 10 and 11. "
                 "For list indexes, test 0, the last index, and one past the end."),
    "equality": ("🟰", "For every `==`, `!=`, `in` or `is` check, test one value that matches and one that doesn't."),
    "math": ("➗", "Check exact calculated numbers with assertEqual, not just that a result exists or is positive."),
    "counters": ("🔢", "When something adds up or counts down (totals, counters, balances), check the exact value "
                 "after several steps."),
    "returns": ("↩️", "Check the return value of every function and method with assertEqual, not only what it changes."),
    "branches": ("🔁", "Test both the yes and the no path of every `if`: one input where it's true and one where it's false."),
    "numbers": ("🔟", "Check exact results that depend on numbers in the code, like starting values, limits and step sizes."),
    "flags": ("✅", "Check True/False results and flags exactly with assertTrue / assertFalse."),
}
MAX_LESSONS_IN_PROMPT = 5
lessons = {}                  # kind -> {"count": jobs where tests missed it, "used": jobs it was given to, "last": date}
lessons_lock = threading.Lock()


def load_lessons():
    lessons.update({k: v for k, v in load_json(LESSONS_FILE, {}).items() if k in LESSONS})


def save_lessons():
    with lessons_lock:
        save_json(LESSONS_FILE, lessons)


def lessons_to_use():
    """The most common lessons so far, most useful first."""
    if not config.get("use_lessons", True):
        return []
    ranked = sorted((k for k, v in lessons.items() if v.get("count", 0) > 0),
                    key=lambda k: -lessons[k]["count"])
    return ranked[:MAX_LESSONS_IN_PROMPT]


def system_prompt(kinds):
    if not kinds:
        return SYSTEM_PROMPT
    tips = "\n".join(f"- {LESSONS[k][1]}" for k in kinds)
    return (SYSTEM_PROMPT + "\n\nLessons from earlier tasks, where tests missed real bugs. "
            "Follow them when writing test_solution.py:\n" + tips)


def learn_from(job):
    """Remember which kinds of planted bugs the tests missed in this job."""
    kinds = sorted({b["kind"] for a in job["attempts"] if a.get("strength")
                    for b in a["strength"]["survived"] if b.get("kind") in LESSONS})
    with lessons_lock:
        for k in kinds:
            entry = lessons.setdefault(k, {"count": 0, "used": 0})
            entry["count"] += 1
            entry["last"] = now()
        tested = any(a.get("strength") for a in job["attempts"])
        for k in job.get("lessons_used", []):
            entry = lessons.setdefault(k, {"count": 0, "used": 0})
            entry["used"] = entry.get("used", 0) + 1
            if tested and k not in kinds:          # given this lesson, the tests caught that kind of bug
                entry["helped"] = entry.get("helped", 0) + 1
    if kinds or job.get("lessons_used"):
        save_lessons()
    return kinds


def first_prompt(job):
    if job["mode"] == "fix":
        goal = job["task"].strip() or "Work out what the code is meant to do from its names and comments."
        return (f"Here is code from a user. It may have bugs.\n\n```python\n{job['code']}\n```\n\n"
                f"What it should do: {goal}{examples_text(job)}\n\n"
                "Write solution.py: the fixed code (keep the same function and class names so it still works "
                "for the user). Write test_solution.py: tests that check it does what it should.")
    return f"Write a Python program for this request:\n\n{job['task']}{examples_text(job)}"


def locked_tests_text(job):
    if not job.get("example_tests"):
        return ""
    return ("\n\nThese tests in test_examples.py were made from the user's own examples. They are LOCKED: they are "
            "always right, you cannot change them, and solution.py must pass them (use the same names they use):\n"
            f"```python\n{job['example_tests']}\n```")


def fix_prompt(job, attempt):
    return (f"The original request was:\n{first_prompt(job)}\n\n"
            f"Here is the current code:\n\n### solution.py\n```python\n{attempt['solution']}\n```\n\n"
            f"### test_solution.py\n```python\n{attempt['tests']}\n```\n\n"
            f"{locked_tests_text(job)}\n\n"
            f"It FAILED. Here is the output:\n```\n{attempt['test_output'][-OUTPUT_LIMIT:]}\n```\n\n"
            "Find the bug and fix it. Fix solution.py. Only change a test in test_solution.py if the test itself is "
            "clearly wrong (for example it expects a wrong answer). Do not delete tests. "
            "Reply with BOTH complete files (solution.py and test_solution.py) again in the same format, "
            "then a short explanation of what you fixed.")


def strengthen_prompt(job, attempt, survived):
    bugs = "\n".join(f"- line {b['line']}: `{b['before']}` was changed to `{b['after']}`" for b in survived[:8])
    return (f"The original request was:\n{first_prompt(job)}\n\n"
            f"Here is the current code:\n\n### solution.py\n```python\n{attempt['solution']}\n```\n\n"
            f"### test_solution.py\n```python\n{attempt['tests']}\n```{locked_tests_text(job)}\n\n"
            "All the tests pass. But when we planted each of these small bugs in solution.py, one at a time, "
            f"the tests STILL passed, so the tests can't tell the good code from buggy code:\n{bugs}\n\n"
            "Add new test methods to test_solution.py that would FAIL with those bugs but PASS with the current "
            "solution.py. Keep all the existing tests. Do NOT change solution.py. If a change makes no difference "
            "to what the code does, skip it. Reply with BOTH files (solution.py unchanged) in the same format, "
            "then say which new tests you added.")


EXAMPLES_SYSTEM = """You turn a user's examples into Python unittest tests. Reply with only this file:

### test_examples.py
```python
(the tests)
```"""


def make_example_tests(job, solution):
    """Turn the user's own examples into tests that the AI isn't allowed to change."""
    goal = job["task"].strip() if job["mode"] == "build" else (job["task"].strip() or "fix this code")
    reply = ask_ai(EXAMPLES_SYSTEM, f"""The user asked for: {goal}

The user's examples. They are the TRUTH. Test exactly what they say, nothing more:
{job['examples'].strip()}

Here is the current solution.py, ONLY so you know the exact function, class and method names to use.
It may have bugs: do not copy its behaviour, follow the user's examples.
```python
{solution}
```

Write test_examples.py:
- Start with: import unittest
  and: from solution import *
- One test method per example: def test_example_1, def test_example_2, ...
- Check exactly the result the user's example says.
- If an example says something should give an error, use: with self.assertRaises(Exception):
- Do not add any other tests.
- End with: if __name__ == "__main__": unittest.main()""")
    m = re.search(r"#+\s*`?test_examples\.py`?\s*\n+```[a-zA-Z0-9_+-]*[ \t]*\n(.*?)```", reply, re.S)
    if m:
        return m.group(1)
    blocks = [b for b in FENCE.findall(reply) if "unittest" in b and "def test" in b]
    return blocks[0] if blocks else None


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

def sandbox_prefix(timeout=TEST_TIMEOUT):
    """Build the command that runs Python locked down: other user, no network, time limit."""
    cmd = []
    if sandbox["user"]:
        cmd += ["sudo", "-n", "-u", SANDBOX_USER]
    cmd += ["/usr/bin/timeout", "-s", "KILL", str(timeout)]
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


def run_sandboxed(files, args, timeout):
    """Write the files to a fresh folder and run python with args in the sandbox. Returns (output, exit code)."""
    folder = tempfile.mkdtemp(prefix="autofix-")
    try:
        os.chmod(folder, 0o777)                      # the sandbox user needs to read and write here
        for name, content in files.items():
            path = os.path.join(folder, name)
            with open(path, "w") as f:
                f.write(content)
            os.chmod(path, 0o644)
        env = {"PATH": "/usr/bin:/bin", "HOME": folder, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            p = subprocess.run(sandbox_prefix(timeout) + args, cwd=folder, env=env, capture_output=True, text=True,
                               errors="replace", timeout=timeout + 10, preexec_fn=limit_resources,
                               stdin=subprocess.DEVNULL)
            return (p.stdout + p.stderr)[-20000:], p.returncode
        except subprocess.TimeoutExpired:
            return "", -9
    finally:
        shutil.rmtree(folder, ignore_errors=True)


RESULT_LINE = re.compile(r"^(test\w*) \((test_solution|test_examples)\.[\w.]+\)(?:\n.*?)? \.\.\. (\w+)", re.M)


def run_tests(solution, tests, example_tests=None, timeout=TEST_TIMEOUT):
    """Run test_solution.py (and the locked test_examples.py). Returns passed, ran, failures, errors, examples, output."""
    files, modules = {"solution.py": solution, "test_solution.py": tests}, ["test_solution"]
    if example_tests:
        files["test_examples.py"] = example_tests
        modules.append("test_examples")
    output, code = run_sandboxed(files, ["-m", "unittest", "-v"] + modules, timeout)
    if code in (-9, 137):
        output += f"\nTIMED OUT: the tests took longer than {timeout} seconds (an infinite loop, or waiting for input()?)"
    ran = int((re.findall(r"^Ran (\d+) tests?", output, re.M) or ["0"])[-1])
    m = re.search(r"^FAILED \((.*)\)", output, re.M)
    counts = dict(re.findall(r"(\w+)=(\d+)", m.group(1))) if m else {}
    failures, errors = int(counts.get("failures", 0)), int(counts.get("errors", 0))
    passed = code == 0 and ran > 0 and re.search(r"^OK\b", output, re.M) is not None
    if ran == 0 and code == 0:
        output += "\nNO TESTS RAN: test_solution.py must contain unittest test methods named test_..."
    examples = None
    if example_tests:
        results = [r for _, mod, r in RESULT_LINE.findall(output) if mod == "test_examples"]
        load_error = re.search(r"^test_examples \(unittest\.loader\._FailedTest", output, re.M) is not None
        examples = {"ran": len(results), "passed": sum(r == "ok" for r in results), "load_error": load_error}
    return {"passed": passed, "ran": ran, "failures": failures, "errors": errors, "examples": examples,
            "output": output}


def run_demo(solution):
    """Run the demo in if __name__ == "__main__": (if there is one). It must not crash."""
    if "__main__" not in solution:
        return None
    output, code = run_sandboxed({"solution.py": solution}, ["solution.py"], DEMO_TIMEOUT)
    if code in (-9, 137):
        output += f"\nTIMED OUT after {DEMO_TIMEOUT} seconds (an infinite loop, or waiting for input()?)"
    return {"ok": code == 0, "output": output[-3000:]}


# ---------- test strength: plant small bugs and check the tests notice ----------

SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult, ast.FloorDiv: ast.Mult,
        ast.Mod: ast.FloorDiv, ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
        ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot,
        ast.IsNot: ast.Is, ast.And: ast.Or, ast.Or: ast.And}


class Mutator(ast.NodeTransformer):
    """Walks the code and changes exactly one spot (number `target`), e.g. < into <= or + into -."""
    def __init__(self, source, target):
        self.source, self.target, self.count, self.change = source, target, 0, None

    def _hit(self):
        hit = self.count == self.target
        self.count += 1
        return hit

    def _record(self, node, new, kind):
        new = ast.copy_location(new, node)
        before = ast.get_source_segment(self.source, node) or ast.unparse(node)
        self.change = {"line": node.lineno, "before": before.strip()[:120], "after": ast.unparse(new)[:120],
                       "kind": kind}
        return new

    def visit_If(self, node):
        t = node.test
        if isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) and t.left.id == "__name__":
            return node                           # don't plant bugs in the demo
        self.generic_visit(node)
        if self._hit():                           # flip the condition: if x: -> if not x:
            new_test = self._record(node.test, ast.UnaryOp(op=ast.Not(), operand=node.test), "branches")
            return ast.If(test=new_test, body=node.body, orelse=node.orelse)
        return node

    def visit_JoinedStr(self, node):              # leave f-strings (printed text) alone
        return node

    def visit_BinOp(self, node):
        self.generic_visit(node)
        new = SWAP.get(type(node.op))
        if new and self._hit():
            return self._record(node, ast.BinOp(left=node.left, op=new(), right=node.right), "math")
        return node

    def visit_AugAssign(self, node):
        self.generic_visit(node)
        new = SWAP.get(type(node.op))
        if new and self._hit():
            return self._record(node, ast.AugAssign(target=node.target, op=new(), value=node.value), "counters")
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        for i, op in enumerate(node.ops):
            new = SWAP.get(type(op))
            if new and self._hit():
                ops = list(node.ops)
                ops[i] = new()
                kind = "boundary" if isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)) else "equality"
                return self._record(node, ast.Compare(left=node.left, ops=ops, comparators=node.comparators), kind)
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        new = SWAP.get(type(node.op))
        if new and self._hit():
            return self._record(node, ast.BoolOp(op=new(), values=node.values), "and_or")
        return node

    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Not) and self._hit():
            return self._record(node, node.operand, "branches")
        return node

    def visit_Constant(self, node):
        if isinstance(node.value, bool):
            if self._hit():
                return self._record(node, ast.Constant(value=not node.value), "flags")
        elif isinstance(node.value, (int, float)):
            if self._hit():
                return self._record(node, ast.Constant(value=node.value + 1), "numbers")
        return node

    def visit_Return(self, node):
        self.generic_visit(node)
        if node.value is not None and not (isinstance(node.value, ast.Constant) and node.value.value is None):
            if self._hit():
                return self._record(node, ast.Return(value=ast.Constant(value=None)), "returns")
        return node


def make_mutants(source):
    """Up to MAX_MUTANTS copies of the code, each with one small bug planted."""
    try:
        counter = Mutator(source, -1)
        counter.visit(ast.parse(source))
    except (SyntaxError, ValueError):
        return []
    total = counter.count
    picks = sorted({round(i * (total - 1) / (MAX_MUTANTS - 1)) for i in range(MAX_MUTANTS)}) if total > MAX_MUTANTS \
        else range(total)
    original = ast.unparse(ast.parse(source))
    mutants = []
    for target in picks:
        m = Mutator(source, target)
        tree = ast.fix_missing_locations(m.visit(ast.parse(source)))
        code = ast.unparse(tree)
        if m.change and code != original:
            mutants.append((code, m.change))
    return mutants


def test_strength(solution, tests, example_tests, progress):
    """Plant bugs one at a time. A good test suite fails ("catches the bug") for each of them."""
    mutants = make_mutants(solution)
    caught, survived = 0, []
    for i, (code, change) in enumerate(mutants, 1):
        progress(i, len(mutants))
        if run_tests(code, tests, example_tests, timeout=10)["passed"]:
            survived.append(change)
        else:
            caught += 1
    score = round(100 * caught / len(mutants)) if mutants else None
    return {"total": len(mutants), "caught": caught, "score": score, "survived": survived}


# ---------- jobs: write, test, fix, repeat ----------

def save_job(job):
    with jobs_lock:
        save_json(os.path.join(JOBS_DIR, job["id"] + ".json"), job)


def run_job(job):
    with work_lock:
        job["status"] = "running"
        save_job(job)
        prev, todo, survived = None, "write", []       # todo: write -> fix (tests failed) / strengthen (tests weak)
        best_score = -1                                 # stop asking for stronger tests once they stop improving
        max_attempts, min_strength = int(config["max_attempts"]), int(config["min_strength"])
        job["lessons_used"] = lessons_to_use()
        prompt_system = system_prompt(job["lessons_used"])
        try:
            for n in range(1, max_attempts + 1):
                if job.get("cancel"):
                    job["status"], job["message"] = "cancelled", "Stopped by you."
                    break
                attempt = {"n": n, "kind": todo, "phase": "ai", "started": time.time(), "model": config["model"]}
                job["attempts"].append(attempt)
                save_job(job)

                prompt = {"write": lambda: first_prompt(job), "fix": lambda: fix_prompt(job, prev),
                          "strengthen": lambda: strengthen_prompt(job, prev, survived)}[todo]()
                t0 = time.time()
                reply = ask_ai(prompt_system, prompt)
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

                if todo == "strengthen":                  # the code already passed: only the tests may change
                    if solution.strip() != prev["solution"].strip():
                        attempt["note"] = "The AI changed the code while making the tests stronger, so the code was kept as it was."
                    solution = prev["solution"]
                # Don't let the AI "fix" things by deleting tests
                if prev is not None and count_tests(tests) < count_tests(prev["tests"]):
                    attempt["note"] = "The AI tried to remove tests, so the earlier tests were kept."
                    tests = prev["tests"]
                elif prev is not None and todo != "strengthen" and tests.strip() != prev["tests"].strip():
                    attempt["tests_changed"] = True

                # Your examples become locked tests (made once, remade only if they don't even load)
                if job.get("examples", "").strip() and (not job.get("example_tests") or job.get("examples_broken")):
                    attempt["phase"] = "examples"
                    save_job(job)
                    t0 = time.time()
                    job["example_tests"] = make_example_tests(job, solution)
                    job["examples_broken"] = False
                    attempt["examples_seconds"] = round(time.time() - t0)
                    attempt["examples_made"] = bool(job["example_tests"])
                    if not job["example_tests"]:
                        attempt["note"] = "Couldn't turn your examples into tests this time. Trying again next attempt."

                attempt.update(solution=solution, tests=tests, example_tests=job.get("example_tests"),
                               phase="testing", test_count=count_tests(tests))
                save_job(job)

                result = run_tests(solution, tests, job.get("example_tests"))
                demo = run_demo(solution) if result["passed"] else None
                output = result["output"]
                if demo and not demo["ok"]:
                    output += "\n\nThe tests passed, but the DEMO (if __name__ == \"__main__\":) CRASHED:\n" + demo["output"]
                attempt.update(passed=result["passed"] and (demo is None or demo["ok"]), ran=result["ran"],
                               failures=result["failures"], errors=result["errors"], examples=result["examples"],
                               demo=demo, test_output=output)
                if result["examples"] and result["examples"]["load_error"]:
                    job["examples_broken"] = True
                prev = attempt
                if not attempt["passed"]:
                    attempt["phase"] = "done"
                    save_job(job)
                    todo = "fix"
                    continue

                # Test strength: are these tests good enough to catch bugs?
                if min_strength > 0:
                    attempt["phase"] = "strength"
                    save_job(job)

                    def progress(i, total):
                        attempt["strength_progress"] = [i, total]
                        save_job(job)
                    attempt["strength"] = test_strength(solution, tests, job.get("example_tests"), progress)
                    attempt.pop("strength_progress", None)
                attempt["phase"] = "done"
                save_job(job)
                st = attempt.get("strength")
                if st and st["total"] and st["score"] < min_strength and n < max_attempts:
                    if st["score"] > best_score:
                        best_score = st["score"]
                        survived, todo = st["survived"], "strengthen"
                        continue
                    attempt["note"] = ("The tests didn't get any stronger, so AutoFix stopped trying. Some planted bugs "
                                       "may make no real difference to what the code does, so no test can catch them.")
                break

            if job["status"] == "running":
                passing = [a for a in job["attempts"] if a.get("passed")]
                if passing:
                    a = passing[-1]
                    job["status"] = "passed"
                    msg = f"All {a['ran']} tests passed on attempt {a['n']}"
                    if a.get("examples"):
                        msg += f", including {a['examples']['passed']} from your examples"
                    if a.get("demo"):
                        msg += ", and the demo ran"
                    st = a.get("strength")
                    if st and st["total"]:
                        msg += f". Test strength {st['score']}%: the tests caught {st['caught']} of {st['total']} planted bugs"
                        if st["score"] < min_strength:
                            msg += ". That's lower than your goal, so read the tests carefully"
                    job["message"] = msg + "."
                else:
                    job["status"] = "failed"
                    job["message"] = (f"Still failing after {max_attempts} attempts. Try describing it more clearly, "
                                      "adding examples, raising the attempt limit, or switching to the smarter model "
                                      "in ⚙️ Settings.")
        except Exception as e:  # AI unreachable, etc.
            job["status"], job["message"] = "error", str(e)
            if job["attempts"] and job["attempts"][-1].get("phase") != "done":
                job["attempts"][-1].update(phase="done", passed=False, note=str(e))
        job["finished"] = now()
        try:
            job["learned"] = learn_from(job)
        except Exception as e:  # never let memory problems break a job
            print("Couldn't update lessons:", e)
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
                    j["status"], j["message"] = "error", "Interrupted because AutoFix restarted."
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
    examples = str(body.get("examples", ""))[:3000]
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
           "code": code, "examples": examples, "title": title, "status": "queued", "created": now(), "attempts": [], "message": ""}
    with jobs_lock:
        jobs[job["id"]] = job
    save_job(job)
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return 200, {"id": job["id"]}


def api_lessons():
    with lessons_lock:
        items = [{"kind": k, "icon": LESSONS[k][0], "text": LESSONS[k][1], **v} for k, v in lessons.items()
                 if k in LESSONS and v.get("count", 0) > 0]
    items.sort(key=lambda x: -x["count"])
    in_use = lessons_to_use()
    for x in items:
        x["in_use"] = x["kind"] in in_use
    return {"enabled": config.get("use_lessons", True), "lessons": items, "max_in_prompt": MAX_LESSONS_IN_PROMPT}


def api_lessons_post(body):
    action = body.get("action")
    if action == "forget" and body.get("kind") in lessons:
        with lessons_lock:
            lessons.pop(body["kind"], None)
        save_lessons()
    elif action == "forget_all":
        with lessons_lock:
            lessons.clear()
        save_lessons()
    elif action == "enable":
        config["use_lessons"] = bool(body.get("enabled"))
        save_json(CONFIG_FILE, config)
    else:
        return 400, {"error": "Unknown action"}
    return 200, api_lessons()


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
    try:
        strength = int(body.get("min_strength", config["min_strength"]))
    except (TypeError, ValueError):
        return 400, {"error": "Test strength goal must be a number"}
    if not 0 <= strength <= 100:
        return 400, {"error": "Test strength goal must be between 0 and 100"}
    if url and not url.startswith("http"):
        url = "http://" + url
    if url and not re.search(r":\d+/?$", url):
        url = url.rstrip("/") + ":11434"
    config.update(ollama_url=url, model=model or DEFAULT_CONFIG["model"], max_attempts=attempts, min_strength=strength)
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
            if path == "/api/lessons":
                return self.send_json(api_lessons())
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
            if path == "/api/lessons":
                return self.send_json(*reversed(api_lessons_post(body)))
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
    load_lessons()
    check_sandbox()
    print(f"Pi AutoFix running! Open http://<your-pi-ip>:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
