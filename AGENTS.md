<!-- BEGIN devskills:base -->
## 1. Think Before Coding

**State assumptions. Surface confusion and tradeoffs; don't pick silently.**

Before implementing:
- State your assumptions explicitly; when something's unclear, name it and ask.
- If multiple interpretations exist, present them — don't choose one silently.
- If a simpler approach exists, say so. Push back when warranted.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.
- Refactor overly long functions without being asked — length alone is a smell worth fixing, even when nothing else is wrong.
- **Comments target humans and explain WHY, not WHAT** — a non-obvious constraint, invariant, or workaround. Default to one line, only where the reason isn't clear from the code; never restate code or cite plan/ticket IDs. A comment past a few lines is rare and signals "this matters" — keep that signal meaningful.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match the codebase's **conventions** — naming, formatting, idioms — **not its deficiencies**. Write what you touch to standard; don't down-level new work to match surrounding code.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

Make those tests count: behavior through the public interface, the failure modes that matter — not coverage, and never pinned to implementation.

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## 5. Safe at the Boundaries

**Distrust the edges. Fail loudly, not silently.**

- Validate untrusted input where it enters — args, request payloads, external API responses. Don't trust it deep inside.
- Handle the errors that can actually happen; propagate or surface the rest. Never swallow an error to make a path look clean.

## 6. Retrieve Just-in-Time

**Pull context on demand. Locate before you read.**

- Search to find the right place; read scoped regions, not whole files "to be safe".
- If `PROJECT.md` exists, read its map first and prefer it over re-deriving structure. When the map and the code disagree, the code wins — reread the file.
- Delegate broad searches to a sub-agent where one is available, so the sweep stays out of your context.
- Sufficiency beats thrift: when unsure, read more. A wrong answer costs far more than the tokens.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
<!-- END devskills:base -->

<!-- BEGIN devskills:language:python -->
<!-- profile: python — managed by devskills; edits between these markers are overwritten -->
## Language Profile — Python

Target: Python 3.13+. Backend services, APIs, CLIs, data pipelines, automation.

Apply these conventions to all Python code in this session.

### Toolchain

Manage the project with `uv` (or `pip` + a virtualenv if the repo already uses it) — never install into the system interpreter. Lint and format with `ruff` (`ruff check` + `ruff format`); set `target-version = "py313"` and enable the `UP` (pyupgrade) rules so legacy typing is rewritten automatically. Type-check with `mypy --strict` (or `pyright`), pinning its Python version to the floor. Test with `pytest`. Pin dependencies in `pyproject.toml`; commit the lockfile.

### Project Layout

`src/<package>/` layout — importable code under `src/`, not the repo root. `tests/` mirrors the package tree. Entrypoints via `[project.scripts]` in `pyproject.toml`, not loose top-level scripts. One responsibility per module; avoid `utils.py`/`helpers.py` grab-bags.

### Typing

- Annotate every function signature and module-level constant. Code is written to pass `mypy --strict` — no implicit `Any`.
- Modern syntax: `list[str]`, `dict[str, int]`, `X | None` over `Optional[X]`. Annotations are evaluated lazily by default on 3.14+ (PEP 649), so forward references need no quoting and `from __future__ import annotations` is redundant; on 3.13 keep that import where it helps.
- PEP 695 type parameters: `class Box[T]:`, `def first[T](xs: list[T]) -> T:`, and the `type Alias = ...` statement, over explicit `TypeVar`/`TypeAlias`. Type-parameter defaults (`[T = int]`) where they simplify call sites *(3.13+)*.
- `@override` (from `typing`) on every method that overrides a base, so the checker catches signature drift. `typing.TypeIs` over `TypeGuard` for narrowing; `ReadOnly[...]` for immutable `TypedDict` items *(3.13+)*.
- `@dataclass(frozen=True, slots=True)` for value objects; `Protocol` for structural interfaces at the point of use; `Enum` over magic strings.
- Mark deprecations with `warnings.deprecated` (`@deprecated`, PEP 702) — it warns at runtime and is read by type checkers. No `# type: ignore` without a trailing reason comment.

### Error Handling

- Catch specific exceptions, never bare `except:` or `except Exception` without re-raising. Preserve the chain with `raise NewError(...) from err`.
- Define a package-level exception hierarchy (`class AppError(Exception)`); don't signal failure with sentinel return values or `None` where an exception is clearer.
- `try` blocks wrap only the line that can fail. Release resources with context managers (`with`), never manual `try/finally` for files, locks, or connections.
- No `return`/`break`/`continue` inside a `finally` block — it silently discards exceptions and pending returns from the `try` (a `SyntaxWarning` on 3.14+, PEP 765).

### Concurrency

- `asyncio` for I/O-bound concurrency; no blocking calls (`time.sleep`, `requests`, sync DB drivers) inside `async def` — use the async client or `asyncio.to_thread`. Every `await` on external I/O has a timeout.
- `asyncio.TaskGroup` over bare `asyncio.gather` for concurrent tasks — scoped lifetime, automatic cancellation of siblings on failure, and `ExceptionGroup` aggregation.
- CPU-bound work goes to `ProcessPoolExecutor` by default — the stock interpreter's GIL serializes threads. Only the separate free-threaded build (`python3.14t`; officially supported per PEP 779 but not the default 3.14 interpreter) runs threads in parallel, so keep `ProcessPoolExecutor` as the portable default. `concurrent.interpreters` / `InterpreterPoolExecutor` (3.14+, PEP 734) is a stdlib subinterpreter option with process-like isolation and less overhead than processes.
- Never mutate shared state across tasks without an `asyncio.Lock`/`threading.Lock` — free-threading makes that data-race discipline matter even for plain threads.

### Testing

- `pytest` with plain `assert`. Parametrize variants with `@pytest.mark.parametrize`; share setup through fixtures, not class hierarchies.
- No real network or filesystem in unit tests — use fakes and `tmp_path`. `pytest.raises` for error paths; `freezegun`/injected clocks instead of real time.
- `pytest-asyncio` for async tests. Test behavior through the public API, not private functions.

### Tiger Style

- Non-trivial functions validate their preconditions (`assert` for invariants, raised exceptions for caller errors and bad input). Don't assert in thin wrappers or trivial accessors.
- All loops over external input have explicit bounds; no unbounded recursion without provable termination.
- No mutable default arguments (`def f(x=[])`) — use `None` and create inside.
<!-- END devskills:language:python -->
