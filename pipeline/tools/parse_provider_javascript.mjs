#!/usr/bin/env node
"use strict";

import process from "node:process";


function argument(name) {
  const position = process.argv.indexOf(name);
  if (position < 0 || position + 1 >= process.argv.length) {
    throw new Error(`missing ${name}`);
  }
  return process.argv[position + 1];
}

function memberName(node) {
  if (!node || typeof node !== "object") return "";
  if (node.type === "Identifier") return node.name;
  if (node.type === "ThisExpression") return "this";
  if (node.type === "MemberExpression") {
    const left = memberName(node.object);
    const right = node.computed && node.property?.type === "Literal" ? node.property.value : memberName(node.property);
    return left && right ? `${left}.${right}` : "";
  }
  return "";
}

function walk(node, visit) {
  if (!node || typeof node !== "object") return;
  if (typeof node.type === "string") visit(node);
  for (const [key, value] of Object.entries(node)) {
    if (key === "start" || key === "end" || key === "loc") continue;
    if (Array.isArray(value)) {
      for (const item of value) walk(item, visit);
    } else {
      walk(value, visit);
    }
  }
}

const acornUrl = argument("--acorn-file-url");
if (!acornUrl.startsWith("file:")) throw new Error("Acorn must be an exact file URL");
const path = argument("--path");
const patterns = JSON.parse(Buffer.from(argument("--patterns-base64"), "base64").toString("utf8"));
const source = await new Promise((resolve, reject) => {
  const chunks = [];
  process.stdin.on("data", (chunk) => chunks.push(chunk));
  process.stdin.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
  process.stdin.on("error", reject);
});
const acorn = await import(acornUrl);
const ast = acorn.parse(source, {ecmaVersion: "latest", sourceType: "module", allowHashBang: true});
const reasons = new Set();
const providerModules = new Set(patterns.provider_modules);
const constructors = new Set(patterns.constructor_names);
const environmentKeys = new Set(patterns.environment_keys);
const hosts = patterns.provider_hosts;

walk(ast, (node) => {
  if ((node.type === "ImportDeclaration" || node.type === "ExportNamedDeclaration") && typeof node.source?.value === "string") {
    if ([...providerModules].some((name) => node.source.value === name || node.source.value.startsWith(`${name}/`))) {
      reasons.add("js-provider-import");
    }
  }
  if (node.type === "CallExpression" || node.type === "NewExpression") {
    const callee = memberName(node.callee);
    const leaf = callee.split(".").at(-1) ?? "";
    if (constructors.has(leaf)) reasons.add("js-provider-constructor");
    if (callee === "fetch") {
      const value = node.arguments?.[0]?.value;
      if (typeof value === "string" && hosts.some((host) => value.includes(host))) reasons.add("js-provider-fetch");
    }
  }
  if (node.type === "MemberExpression") {
    const name = memberName(node);
    if (name.startsWith("process.env.") && environmentKeys.has(name.slice("process.env.".length))) {
      reasons.add("js-provider-env-key");
    }
  }
});

process.stdout.write(`${JSON.stringify({path, reasons: [...reasons].sort()})}\n`);
