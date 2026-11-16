#!/usr/bin/env node
// Comment lint for a product repo: runs under node or bun with no dependency but the ast-grep binary.
// Rules: https://github.com/KOCEAN33/llm-wiki/blob/main/practices/code-comments.md
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const LANGUAGES = ['python', 'typescript', 'tsx', 'javascript', 'go', 'bash', 'yaml'];
const MIN_SENTENCE = 35;

const URL = /\bhttps?:\/\/\S+/g;
// A relative path with a directory and an extension. An absolute or home path names a place on a
// machine, not a file of this repository, so it passes.
const REPO_PATH = /(?<![\w/~.-])(?:\.{1,2}\/)?(?:[\w@.-]+\/)+[\w.-]+\.[A-Za-z]{1,5}\b/;

const PATH_MESSAGE =
  'A comment cites a file path or a doc section. Name the symbol instead ({@link Name}, [Name], or `name`), or cite an ADR id or an external URL.';

// Each directive, what counts as its reason, and the form the message shows.
const SUPPRESSIONS = [
  { directive: /#\s*(?:ruff:\s*)?noqa\b(?::\s*[A-Z0-9, ]+)?/, reason: /^\s*-+\s*\S/, form: '# noqa: <code> -- <reason>' },
  { directive: /#\s*(?:type|pyright):\s*ignore\b(?:\[[^\]]*\])?/, reason: /^\s*-+\s*\S/, form: '# type: ignore[<code>] -- <reason>' },
  { directive: /#\s*shellcheck\s+disable=[\w,]+/, reason: /^\s*#\s*\S/, form: '# shellcheck disable=<code> # <reason>' },
  { directive: /(?:eslint|oxlint)-disable(?:-next-line|-line)?\b/, reason: /\s--\s+\S/, form: '// oxlint-disable-next-line <rule> -- <reason>' },
  { directive: /@ts-(?:expect-error|ignore|nocheck)\b/, reason: /^[\s:-]*\w/, form: '// @ts-expect-error <reason>' },
  { directive: /\/\/\s*nolint\b(?::[\w,]+)?/, reason: /^\s*\/\/\s*\S/, form: '//nolint:<linter> // <reason>' },
];

const strip = (text) =>
  text
    .split('\n')
    .map((line) => line.replace(/^\s*(?:\/\*+|\*+\/|\*|\/\/+|#+)\s?/, '').replace(/\s*\*+\/\s*$/, ''))
    .join(' ')
    .replace(/\s+/g, ' ')
    .trim();

function citesPath(comment) {
  const text = comment.text.replace(URL, '');
  return text.includes('§') || REPO_PATH.test(text) ? PATH_MESSAGE : null;
}

function missingReason(comment) {
  for (const { directive, reason, form } of SUPPRESSIONS) {
    const m = directive.exec(comment.text);
    // A directive opens its comment. One further in is prose about the directive.
    if (!m || /\w/.test(comment.text.slice(0, m.index).replace(/^\s*(?:\/\/+|\/\*+|#+)/, ''))) continue;
    if (!reason.test(comment.text.slice(m.index + m[0].length))) return `A lint suppression has no reason. Write it after the directive: ${form}`;
  }
  return null;
}

// Comments on adjacent lines of one file read as one block, so a sentence may wrap.
function blocks(comments) {
  const sorted = [...comments].sort((a, b) => (a.file === b.file ? a.line - b.line : a.file < b.file ? -1 : 1));
  const out = [];
  for (const c of sorted) {
    const last = out[out.length - 1];
    const lines = c.text.split('\n').length;
    if (last && last.file === c.file && c.line === last.end + 1) {
      last.text += ' ' + strip(c.text);
      last.end = c.line + lines - 1;
    } else out.push({ file: c.file, line: c.line, end: c.line + lines - 1, text: strip(c.text) });
  }
  return out;
}

function duplicates(comments) {
  const seen = new Map();
  for (const b of blocks(comments)) {
    for (const sentence of b.text.split(/(?<=[.!?])\s+/)) {
      if (sentence.length < MIN_SENTENCE) continue;
      if (!seen.has(sentence)) seen.set(sentence, []);
      seen.get(sentence).push(b);
    }
  }
  const out = [];
  for (const [sentence, where] of seen) {
    const files = [...new Set(where.map((b) => b.file))];
    if (files.length < 2) continue;
    for (const file of files) {
      const first = where.find((b) => b.file === file);
      const others = files.filter((f) => f !== file).join(', ');
      out.push({
        rule: 'duplicate-sentence',
        file,
        line: first.line,
        text: sentence,
        message: `The same comment sentence is also in ${others}. Keep it on the symbol that owns the fact and name that symbol here.`,
      });
    }
  }
  return out;
}

export function lintComments(comments) {
  const out = [];
  for (const c of comments) {
    for (const [rule, check] of [['cites-path', citesPath], ['suppression-reason', missingReason]]) {
      const message = check(c);
      if (message) out.push({ rule, file: c.file, line: c.line, text: strip(c.text), message });
    }
  }
  return [...out, ...duplicates(comments)];
}

function extract(astGrep, paths) {
  const [bin, ...pre] = astGrep.split(' ');
  const comments = [];
  for (const lang of LANGUAGES) {
    const run = spawnSync(bin, [...pre, 'run', '--kind', 'comment', '--lang', lang, '--json=stream', ...paths], {
      encoding: 'utf8',
      maxBuffer: 1 << 28,
    });
    if (run.error || run.status > 1) throw new Error(`${astGrep} failed for ${lang}: ${run.error?.message ?? run.stderr}`);
    for (const row of run.stdout.split('\n').filter(Boolean)) {
      const { file, text, range } = JSON.parse(row);
      comments.push({ file, line: range.start.line + 1, text });
    }
  }
  return comments;
}

const key = (v) => `${v.rule}:${v.file}:${createHash('sha1').update(v.text).digest('hex').slice(0, 12)}`;

function main(argv) {
  const opt = { astGrep: 'ast-grep', baseline: '.comment-lint-baseline.json', mode: 'check', paths: [] };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--ast-grep') opt.astGrep = argv[++i];
    else if (argv[i] === '--baseline') opt.baseline = argv[++i];
    else if (argv[i] === '--write-baseline') opt.mode = 'write';
    else if (argv[i] === '--prune-baseline') opt.mode = 'prune';
    else opt.paths.push(argv[i]);
  }
  const found = lintComments(extract(opt.astGrep, opt.paths.length ? opt.paths : ['.']));
  const current = new Set(found.map(key));
  if (opt.mode === 'write') {
    writeFileSync(opt.baseline, JSON.stringify([...current].sort(), null, 2) + '\n');
    console.log(`comment-lint: wrote ${current.size} baseline entries`);
    return 0;
  }
  const baseline = existsSync(opt.baseline) ? JSON.parse(readFileSync(opt.baseline, 'utf8')) : [];
  const stale = baseline.filter((k) => !current.has(k));
  if (opt.mode === 'prune') {
    writeFileSync(opt.baseline, JSON.stringify(baseline.filter((k) => current.has(k)), null, 2) + '\n');
    console.log(`comment-lint: pruned ${stale.length} baseline entries`);
    return 0;
  }
  const known = new Set(baseline);
  const fresh = found.filter((v) => !known.has(key(v)));
  for (const v of fresh) console.error(`${v.file}:${v.line}: [${v.rule}] ${v.message}\n    ${v.text}`);
  if (stale.length) console.error(`comment-lint: ${stale.length} baseline entries match no comment. Run with --prune-baseline.`);
  if (fresh.length) console.error(`comment-lint: ${fresh.length} violations`);
  return fresh.length || stale.length ? 1 : 0;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) process.exit(main(process.argv.slice(2)));
