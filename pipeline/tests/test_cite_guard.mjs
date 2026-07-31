// T2-3 DR citation guard — standalone Node test.
//
// Verifies the hallucinated-citation guard added to the BASIC Deep Research
// finalize path in pipeline/build_topic_index.py (function
// `guardDanglingCitations`). Two layers of verification:
//
//   1. Logic: a COPY of the injected pure function is exercised with synthetic
//      inputs. The copy below MUST stay byte-for-byte equivalent (modulo the
//      Python-string escaping of the embedded regex) to what is injected into
//      build_topic_index.py.
//   2. Emission: the real JS template is extracted from build_topic_index.py
//      (via a tiny Python ast helper) into a scratch file and `node --check`'d,
//      proving the edited template still parses as valid JavaScript.
//
// Run:  node pipeline/tests/test_cite_guard.mjs
// Read-only / scratch-only; never writes into docs/ or the repo.

import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve, join } from "node:path";
import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(__dirname, "..", "..");
const TARGET = join(REPO, "pipeline", "build_topic_index.py");

let failures = 0;
function assert(cond, msg) {
  if (cond) { console.log("  ok  - " + msg); }
  else { console.error("  FAIL- " + msg); failures++; }
}

// --- COPY of the injected pure function (must match build_topic_index.py) ---
// In the Python triple-quoted template the regex is written as
// /\\[ref:(\\d+)\\]/g; Python decodes the double backslashes, so the emitted
// JS — and therefore this copy — uses single backslashes: /\[ref:(\d+)\]/g.
function guardDanglingCitations(answer, refs) {
  const text = String(answer || "");
  const list = refs || [];
  const dropped = [];
  const seen = new Set();
  for (const m of text.matchAll(/\[ref:(\d+)\]/g)) {
    const n = parseInt(m[1], 10);
    if (list[n - 1]) continue;
    if (!seen.has(n)) { seen.add(n); dropped.push(n); }
  }
  if (!dropped.length) return { answer: text, dropped: [], changed: false };
  const bad = new Set(dropped);
  const cleaned = text.replace(/\[ref:(\d+)\]/g, function(mk, n) {
    return bad.has(parseInt(n, 10)) ? "" : mk;
  });
  return { answer: cleaned, dropped: dropped.sort(function(a, b) { return a - b; }), changed: true };
}

console.log("[1] logic: dangling [ref:9] with only refs 1..8 available");
{
  const refs = Array.from({ length: 8 }, (_, i) => ({ n: i + 1, title: "P" + (i + 1) }));
  const answer = "Alpha[ref:1]. Beta[ref:2]. Gamma claims something[ref:9].";
  const r = guardDanglingCitations(answer, refs);
  assert(r.changed === true, "changed flag is true when a dangling ref exists");
  assert(JSON.stringify(r.dropped) === "[9]", "ref 9 is reported as dropped (got " + JSON.stringify(r.dropped) + ")");
  assert(!/\[ref:9\]/.test(r.answer), "[ref:9] marker is stripped from the answer");
  assert(/\[ref:1\]/.test(r.answer) && /\[ref:2\]/.test(r.answer), "valid [ref:1]/[ref:2] are preserved");
  assert(r.dropped.length > 0, "a warning payload (dropped list) is produced");
}

console.log("[2] happy path: only valid refs -> output unchanged (byte-identical)");
{
  const refs = Array.from({ length: 8 }, (_, i) => ({ n: i + 1, title: "P" + (i + 1) }));
  const answer = "Alpha[ref:1]. Beta[ref:2]. Delta[ref:8].";
  const r = guardDanglingCitations(answer, refs);
  assert(r.changed === false, "changed flag is false when all refs are valid");
  assert(r.answer === answer, "answer is returned byte-identical to the input");
  assert(r.dropped.length === 0, "no dropped refs reported");
}

console.log("[3] edge: multiple distinct dangling refs deduped + sorted");
{
  const refs = Array.from({ length: 3 }, (_, i) => ({ n: i + 1, title: "P" + (i + 1) }));
  const answer = "A[ref:5] B[ref:1] C[ref:5] D[ref:4] E[ref:0]";
  const r = guardDanglingCitations(answer, refs);
  assert(r.changed === true, "changed true with several dangling refs");
  assert(JSON.stringify(r.dropped) === "[0,4,5]", "dropped is deduped + ascending (got " + JSON.stringify(r.dropped) + ")");
  assert(r.answer.indexOf("[ref:1]") !== -1, "the single valid ref [ref:1] survives");
  assert(!/\[ref:5\]|\[ref:4\]|\[ref:0\]/.test(r.answer), "all dangling markers removed");
}

console.log("[4] robustness: empty / missing inputs never throw");
{
  const a = guardDanglingCitations("", []);
  assert(a.changed === false && a.answer === "", "empty answer -> no change");
  const b = guardDanglingCitations("no citations here", null);
  assert(b.changed === false, "answer with no [ref:N] and null refs -> no change");
  const c = guardDanglingCitations(null, undefined);
  assert(c.changed === false && c.answer === "", "null answer + undefined refs -> safe no-op");
}

console.log("[5] emission: extract real JS template + node --check");
{
  let checked = false;
  try {
    const py = process.env.PAPER_CURATION_PY312
      || "/opt/homebrew/Caskroom/miniconda/base/envs/py312/bin/python";
    const extractor = resolve(REPO, "..", "scratch_extract_js.py");
    // Prefer the session scratch extractor if present; otherwise inline a tiny one.
    const scratchDir = mkdtempSync(join(tmpdir(), "citeguard-"));
    const outJs = join(scratchDir, "deep_research_template.js");
    const inlineExtractor = join(scratchDir, "extract_js.py");
    const pyCode = [
      "import ast, sys",
      "src=open(sys.argv[1],encoding='utf-8').read()",
      "m=src.index('JS = \"\"\"')",
      "bs=src.index('\"\"\"',m)",
      "be=src.index('\"\"\"',bs+3)",
      "js=ast.literal_eval(src[bs:be+3])",
      "open(sys.argv[2],'w',encoding='utf-8').write(js)",
      "assert 'guardDanglingCitations' in js",
      "assert 'deepRenderCiteWarning' in js",
      "print('extracted',len(js),'chars; guard present')",
    ].join("\n");
    const { writeFileSync } = await import("node:fs");
    writeFileSync(inlineExtractor, pyCode, "utf-8");
    const pyInterp = existsSync(py) ? py : "python3";
    const extractOut = execFileSync(pyInterp, [inlineExtractor, TARGET, outJs], { encoding: "utf-8" });
    console.log("  " + extractOut.trim().split("\n").join("\n  "));
    // node --check throws on parse error; success => exit 0.
    execFileSync(process.execPath, ["--check", outJs], { encoding: "utf-8" });
    console.log("  ok  - node --check passed on extracted Deep Research JS template");
    checked = true;
  } catch (e) {
    console.error("  FAIL- emission check: " + (e && e.message ? e.message.split("\n")[0] : e));
    failures++;
  }
  assert(checked, "emitted JS template extracted and parsed");
}

if (failures) { console.error("\nFAILED: " + failures + " assertion(s)"); process.exit(1); }
console.log("\nALL PASS");
