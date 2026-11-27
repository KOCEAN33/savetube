#!/usr/bin/env node
// Weekly periodic checks: fixed rule lists per language, per-rule counts appended to a TSV for the
// trend, and only the findings and import cycles new since the last run written as a work item.
// Rules: https://github.com/KOCEAN33/llm-wiki/blob/main/practices/periodic-code-checks.md
//
//   node run.mjs skip   prints skip=true when no commit but this workflow's own landed since the last run
//   node run.mjs run    runs every tool and writes the counts, the last outputs, and any work item
//
// The npm tools come from this directory's lockfile (npm ci), the Python tools from uvx at the pins below.
import { spawnSync } from 'node:child_process';
import { appendFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..', '..');
const BIN = join(HERE, 'node_modules', '.bin');
const PYRIGHT_CONFIG = relative(ROOT, join(HERE, 'pyrightconfig.json'));
const RUFF = 'ruff@0.17.6';
const BANDIT = 'bandit==1.9.4';
const BANDIT_PACKAGE = 'backend/savetube';
const BOT = { name: 'github-actions[bot]', email: '41898282+github-actions[bot]@users.noreply.github.com' };

const CHECKS_DIR = 'docs/checks';
const COUNTS = CHECKS_DIR + '/counts.tsv';
const LAST = CHECKS_DIR + '/last';
const NEXT_INDEX = 'docs/NEXT.md';
const COUNTS_HEADER = 'date\tcommit\ttool\trule\tcount';

// Test files and generated files, left out of every tool. The shadcn components and the Alembic
// revisions are generated, and the check outputs would otherwise count as duplication.
const EXCLUDED_DIRS = [
  'backend/tests',
  'e2e',
  'backend/savetube/migrations/versions',
  'frontend/src/components/ui',
  CHECKS_DIR,
];
const EXCLUDED_FILES = [
  '**/*.test.*',
  '**/*.spec.*',
  '**/test_*.py',
  '**/conftest.py',
  '**/dist/**',
  '**/node_modules/**',
  '**/pnpm-lock.yaml',
  '**/package-lock.json',
  '**/uv.lock',
  '.comment-lint-baseline.json',
];
const EXCLUDED_GLOBS = [...EXCLUDED_DIRS.map((d) => d + '/**'), ...EXCLUDED_FILES];

const OXLINT_RULES = [
  'no-empty',
  'no-useless-catch',
  'typescript/no-floating-promises',
  'typescript/only-throw-error',
  'typescript/no-misused-promises',
  'import/no-cycle',
];
const RUFF_SELECT = ['BLE001', 'E722', 'S110', 'S112', 'TRY'];
const RUFF_FIXED_CODES = RUFF_SELECT.filter((c) => /\d$/.test(c));
const CYCLE_RULE = 'reportImportCycles';
const DUPLICATION_RULE = 'duplicated-lines-percentage';

function sh(cmd, args, env = {}) {
  const started = Date.now();
  const r = spawnSync(cmd, args, {
    cwd: ROOT,
    encoding: 'utf8',
    maxBuffer: 1 << 30,
    env: { ...process.env, ...env },
  });
  if (r.error) throw r.error;
  // Each tool exits 1 when it reports a finding.
  if (r.status !== 0 && r.status !== 1) {
    throw new Error(cmd + ' ' + args.join(' ') + ' exited ' + r.status + '\n' + r.stderr + r.stdout);
  }
  // Ruff and Pyright print absolute paths. Repository-relative ones compare across machines.
  return { out: r.stdout.split(ROOT + '/').join(''), ms: Date.now() - started };
}

function git(...args) {
  const r = spawnSync('git', args, { cwd: ROOT, encoding: 'utf8' });
  return { ok: r.status === 0, out: r.stdout.trim() };
}

const at = (p) => join(ROOT, p);
const lastPath = (name) => LAST + '/' + name;

function readLast(name) {
  return existsSync(at(lastPath(name))) ? JSON.parse(readFileSync(at(lastPath(name)), 'utf8')) : null;
}

function writeLast(name, text) {
  mkdirSync(at(LAST), { recursive: true });
  writeFileSync(at(lastPath(name)), text);
}

function readCounts() {
  if (!existsSync(at(COUNTS))) return [];
  return readFileSync(at(COUNTS), 'utf8')
    .split('\n')
    .slice(1)
    .filter(Boolean)
    .map((line) => {
      const [date, commit, tool, rule, count] = line.split('\t');
      return { date, commit, tool, rule, count };
    });
}

const oneLine = (s) => s.replace(/\s*\n\s*/g, ' ').trim();

// A rule keeps a row after its findings are gone, so the trend shows the drop to zero.
function tally(fixedRules, previousRows, tool, foundRules) {
  const counts = new Map(fixedRules.map((r) => [r, 0]));
  for (const row of previousRows) if (row.tool === tool) counts.set(row.rule, 0);
  for (const r of foundRules) counts.set(r, (counts.get(r) ?? 0) + 1);
  return counts;
}

function newOnly(current, previous, key) {
  if (previous === null) return [];
  const seen = new Set(previous.map(key));
  return current.filter((f) => !seen.has(key(f)));
}

// Oxlint codes read "typescript(no-floating-promises)"; the CLI names that rule
// "typescript/no-floating-promises", and an ESLint core rule by its bare name.
function oxlintRule(code) {
  const [, plugin, name] = /^([\w-]+)\((.+)\)$/.exec(code);
  return plugin === 'eslint' ? name : plugin + '/' + name;
}

// Both tools report one cycle once per member file, each list starting at that file. Rotating the
// list to start at its smallest path gives one key per cycle.
function cycleKeys(fileLists) {
  const cycles = new Map();
  for (const files of fileLists) {
    const start = files.indexOf([...files].sort()[0]);
    const chain = [...files.slice(start), ...files.slice(0, start)];
    cycles.set([...chain, chain[0]].join(' -> '), chain);
  }
  return cycles;
}

function newCycleItems(cycles, previous, tool, rule) {
  if (previous === null) return [];
  return [...cycles]
    .filter(([key]) => !previous.has(key))
    .map(([key, chain]) => ({ tool, rule, where: chain[0], message: 'import cycle: ' + key }));
}

const OXLINT_CYCLE_RULE = 'import/no-cycle';

// The no-cycle message is the same for every cycle. The note names the other files of the cycle.
function oxlintCycles(diagnostics) {
  return cycleKeys(
    diagnostics
      .filter((d) => oxlintRule(d.code) === OXLINT_CYCLE_RULE)
      .map((d) => [d.filename, ...[...(d.note ?? '').matchAll(/\(([^()]+)\)/g)].map((m) => m[1])]),
  );
}

function oxlint({ previousRows }) {
  const previous = readLast('oxlint.json');
  const run = sh(
    join(BIN, 'oxlint'),
    [
      '--type-aware',
      '--import-plugin',
      '--disable-nested-config',
      '-A',
      'all',
      ...OXLINT_RULES.flatMap((r) => ['-D', r]),
      '--format',
      'json',
      ...EXCLUDED_GLOBS.flatMap((g) => ['--ignore-pattern', g]),
      '.',
    ],
    { OXLINT_TSGOLINT_PATH: join(BIN, 'tsgolint') },
  );
  const output = JSON.parse(run.out);
  const found = output.diagnostics;
  // Oxlint prints diagnostics in thread completion order. Sorted, two runs diff only by their findings.
  const position = (d) => [d.filename, String(d.labels?.[0]?.span?.offset ?? 0).padStart(9, '0'), d.code].join('\0');
  found.sort((a, b) => position(a).localeCompare(position(b)));
  writeLast('oxlint.json', JSON.stringify(output, null, 2) + '\n');
  const findings = found.filter((d) => oxlintRule(d.code) !== OXLINT_CYCLE_RULE);
  const cycles = oxlintCycles(found);
  const key = (d) => [d.code, d.filename, d.message].join('\0');
  const previousFindings = previous?.diagnostics.filter((d) => oxlintRule(d.code) !== OXLINT_CYCLE_RULE) ?? null;
  const items = [
    ...newOnly(findings, previousFindings, key).map((d) => ({
      tool: 'oxlint',
      rule: oxlintRule(d.code),
      where: d.filename + ':' + (d.labels?.[0]?.span?.line ?? 1),
      message: oneLine(d.message),
    })),
    ...newCycleItems(cycles, previous && oxlintCycles(previous.diagnostics), 'oxlint', OXLINT_CYCLE_RULE),
  ];
  const counts = tally(OXLINT_RULES, previousRows, 'oxlint', findings.map((d) => oxlintRule(d.code)));
  counts.set(OXLINT_CYCLE_RULE, cycles.size);
  return { tool: 'oxlint', counts, items, ms: run.ms };
}

function ruff({ previousRows }) {
  const previous = readLast('ruff.json');
  const run = sh('uvx', [
    RUFF,
    'check',
    '--isolated',
    '--select',
    RUFF_SELECT.join(','),
    '--output-format',
    'json',
    '--extend-exclude',
    EXCLUDED_GLOBS.join(','),
    '.',
  ]);
  const found = JSON.parse(run.out);
  writeLast('ruff.json', run.out);
  const key = (f) => [f.code, f.filename, f.message].join('\0');
  const items = newOnly(found, previous, key).map((f) => ({
    tool: 'ruff',
    rule: f.code,
    where: f.filename + ':' + f.location.row,
    message: f.message,
  }));
  return { tool: 'ruff', counts: tally(RUFF_FIXED_CODES, previousRows, 'ruff', found.map((f) => f.code)), items, ms: run.ms };
}

function bandit({ previousRows }) {
  const args = ['--from', BANDIT, 'bandit', '-q', '-r', BANDIT_PACKAGE, '-x', EXCLUDED_GLOBS.join(','), '-f', 'json'];
  let items = [];
  let ms = 0;
  // The new-only run reads the last output as its baseline, so it runs before the full run replaces it.
  if (readLast('bandit.json') !== null) {
    const fresh = sh('uvx', [...args, '-b', lastPath('bandit.json')]);
    ms += fresh.ms;
    items = JSON.parse(fresh.out).results.map((f) => ({
      tool: 'bandit',
      rule: f.test_id,
      where: f.filename + ':' + f.line_number,
      message: f.issue_text,
    }));
  }
  const run = sh('uvx', args);
  ms += run.ms;
  writeLast('bandit.json', run.out);
  const found = JSON.parse(run.out).results;
  return { tool: 'bandit', counts: tally([], previousRows, 'bandit', found.map((f) => f.test_id)), items, ms };
}

// The message lists the cycle's files, one per line after the first.
function pyrightCycles(output) {
  return cycleKeys(
    output.generalDiagnostics
      .filter((d) => d.rule === CYCLE_RULE)
      .map((d) => d.message.split('\n').slice(1).map((line) => line.trim())),
  );
}

function pyright({ previousRows }) {
  const previous = readLast('pyright.json');
  const run = sh(join(BIN, 'pyright'), ['--outputjson', '-p', PYRIGHT_CONFIG]);
  const cycles = pyrightCycles(JSON.parse(run.out));
  writeLast('pyright.json', run.out);
  const items = newCycleItems(cycles, previous && pyrightCycles(previous), 'pyright', CYCLE_RULE);
  const counts = tally([CYCLE_RULE], previousRows, 'pyright', []);
  counts.set(CYCLE_RULE, cycles.size);
  return { tool: 'pyright', counts, items, ms: run.ms };
}

function jscpd({ lastCommit }) {
  const args = ['--reporters', 'json', '--silent', '--ignore', EXCLUDED_GLOBS.join(','), '.'];
  let items = [];
  let ms = 0;
  if (lastCommit && existsSync(at(lastPath('jscpd')))) {
    const out = mkdtempSync(join(tmpdir(), 'jscpd-new-'));
    ms += sh(join(BIN, 'jscpd'), [...args, '--output', out, '--baseline-from-ref', lastCommit]).ms;
    const report = JSON.parse(readFileSync(join(out, 'jscpd-report.json'), 'utf8'));
    rmSync(out, { recursive: true });
    items = report.duplicates
      .filter((c) => c.isNew)
      .map((c) => ({
        tool: 'jscpd',
        rule: 'clone',
        where: c.firstFile.name + ':' + c.firstFile.start,
        message: c.lines + ' lines duplicated at ' + c.secondFile.name + ':' + c.secondFile.start,
      }));
  }
  rmSync(at(lastPath('jscpd')), { recursive: true, force: true });
  ms += sh(join(BIN, 'jscpd'), [...args, '--output', lastPath('jscpd')]).ms;
  const report = JSON.parse(readFileSync(at(lastPath('jscpd') + '/jscpd-report.json'), 'utf8'));
  const percentage = Math.round(report.statistics.total.percentage * 100) / 100;
  return { tool: 'jscpd', counts: new Map([[DUPLICATION_RULE, percentage]]), items, ms };
}

function requireInstalled(path, command) {
  if (!existsSync(at(path))) throw new Error(path + ' is missing. Run: ' + command);
}

function writeCounts(results, date, commit, previousAll) {
  // A second run on one commit replaces that commit's rows, so the trend keeps one reading per commit.
  const kept = previousAll.filter((r) => r.commit !== commit);
  const fresh = results.flatMap((res) =>
    [...res.counts].sort(([a], [b]) => a.localeCompare(b)).map(([rule, count]) => ({ date, commit, tool: res.tool, rule, count })),
  );
  const lines = [...kept, ...fresh].map((r) => [r.date, r.commit, r.tool, r.rule, r.count].join('\t'));
  mkdirSync(at(CHECKS_DIR), { recursive: true });
  writeFileSync(at(COUNTS), [COUNTS_HEADER, ...lines].join('\n') + '\n');
}

function writeWorkItem(items, date, commit, lastCommit) {
  const path = 'docs/next/periodic-checks-' + date + '.md';
  const body = [
    '# Periodic checks ' + date,
    '',
    'Findings and import cycles at ' + commit.slice(0, 12) + ' that the last run, at ' + lastCommit.slice(0, 12) + ', did not report. One line each: tool, rule, file:line, message.',
    '',
    ...items.map((i) => '- ' + i.tool + ', ' + i.rule + ', ' + '\x60' + i.where + '\x60' + ', ' + i.message),
    '',
  ];
  mkdirSync(at('docs/next'), { recursive: true });
  writeFileSync(at(path), body.join('\n'));
  if (!existsSync(at(NEXT_INDEX))) {
    console.log(NEXT_INDEX + ' does not exist: wrote ' + path + ' only');
    return path;
  }
  const index = readFileSync(at(NEXT_INDEX), 'utf8');
  if (!index.includes(path)) {
    const line = '- Periodic checks ' + date + ': ' + items.length + ' new findings since the last weekly run. Detail: ' + '\x60' + path + '\x60' + '.';
    writeFileSync(at(NEXT_INDEX), index.replace(/\n*$/, '\n') + line + '\n');
  }
  return path;
}

function skip() {
  const lastCommit = readCounts().at(-1)?.commit;
  // A commit missing from history, such as one dropped by a force push, makes the log fail: run then.
  const log = lastCommit ? git('log', '--format=%ae', lastCommit + '..HEAD') : { ok: false };
  const authors = log.ok ? log.out.split('\n').filter(Boolean) : [];
  const skipped = log.ok && authors.every((email) => email === BOT.email);
  console.log(skipped ? 'skip: no commit since the last run at ' + lastCommit : 'run: new commits since ' + (lastCommit ?? 'no run yet'));
  if (process.env.GITHUB_OUTPUT) {
    appendFileSync(process.env.GITHUB_OUTPUT, 'skip=' + skipped + '\nbot_name=' + BOT.name + '\nbot_email=' + BOT.email + '\n');
  }
}

function runChecks() {
  requireInstalled(relative(ROOT, BIN), 'npm ci --prefix ' + relative(ROOT, HERE));
  // Without the frontend's dependencies the type-aware rules see unresolved types and report less.
  requireInstalled('frontend/node_modules', 'pnpm install --dir frontend --frozen-lockfile');
  const commit = git('rev-parse', 'HEAD').out;
  const date = new Date().toISOString().slice(0, 10);
  const previousAll = readCounts();
  const lastRow = previousAll.filter((r) => r.commit !== commit).at(-1) ?? previousAll.at(-1);
  const lastCommit = lastRow?.commit;
  const previousRows = previousAll.filter((r) => r.commit === lastCommit);
  const ctx = { lastCommit, previousRows };

  const results = [oxlint, ruff, bandit, pyright, jscpd].map((check) => check(ctx));
  writeCounts(results, date, commit, previousAll);

  // With no earlier run everything is the baseline, so nothing is new.
  const items = lastCommit ? results.flatMap((r) => r.items) : [];
  for (const r of results) {
    const counts = [...r.counts].map(([rule, n]) => rule + '=' + n).join(' ');
    console.log(r.tool.padEnd(8) + String(r.ms).padStart(7) + ' ms  new=' + r.items.length + '  ' + counts);
  }
  if (items.length === 0) {
    console.log(lastCommit ? 'no new findings since ' + lastCommit : 'baseline run: counts only');
    return;
  }
  console.log(items.length + ' new findings: ' + writeWorkItem(items, date, commit, lastCommit));
}

const command = process.argv[2];
if (command === 'skip') skip();
else if (command === 'run') runChecks();
else {
  console.error('usage: run.mjs skip|run');
  process.exit(2);
}
