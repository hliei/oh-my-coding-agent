import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  copyFileSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";


const REQUIRED_NODE = "v22.19.0";
const REQUIRED_NODE_SHA256 =
  "0d005c18e095027ca8f9fe1cc1126f767ee4ad7b004f4d6fb781ec4228a7c5d1";
const REQUIRED_NPM = "10.9.3";
const SANDBOX = "/usr/bin/sandbox-exec";
const SANDBOX_PROFILE = "(version 1)(allow default)(deny network*)";

if (process.version !== REQUIRED_NODE) {
  throw new Error(
    `Reference Capture Row requires Node ${REQUIRED_NODE}, received ${process.version}`,
  );
}
const nodeSha256 = createHash("sha256")
  .update(readFileSync(process.execPath))
  .digest("hex");
if (nodeSha256 !== REQUIRED_NODE_SHA256) {
  throw new Error(
    `Reference Capture Row requires Node binary ${REQUIRED_NODE_SHA256}, received ${nodeSha256}`,
  );
}
if (process.argv.length !== 5) {
  throw new Error(
    "usage: node capture_reference_corpus.mjs PYTHON MANIFEST REFERENCE_EXPORT",
  );
}

const [, , python, manifest, referenceExport] = process.argv;
const captureRoot = mkdtempSync(join(tmpdir(), "omh-reference-capture-"));
const captureOutput = join(captureRoot, "runtime-capture.jsonl");
writeFileSync(captureOutput, "", "utf-8");
const captureTest = fileURLToPath(
  new URL(
    "./reference_capture_tests/session-scoped-extension-modules.test.ts",
    import.meta.url,
  ),
);
const installedCaptureTest = join(
  referenceExport,
  "packages/coding-agent/test/omh-reference-session-scoped-extension-modules.test.ts",
);
rmSync(installedCaptureTest, { force: true });
const cleanup = () => {
  rmSync(installedCaptureTest, { force: true });
  rmSync(captureRoot, { recursive: true, force: true });
};
const fail = (completed) => {
  process.stdout.write(completed.stdout ?? "");
  process.stderr.write(completed.stderr ?? "");
  cleanup();
  process.exit(completed.status ?? 1);
};
const runIsolated = (command, args, options) =>
  spawnSync(SANDBOX, ["-p", SANDBOX_PROFILE, command, ...args], options);
const cleanEnvironment = {
  PATH: process.env.PATH,
  HOME: process.env.HOME,
  LANG: "C.UTF-8",
  LC_ALL: "C.UTF-8",
  TZ: "UTC",
};
const npmVersion = runIsolated("npm", ["--version"], {
  env: cleanEnvironment,
  encoding: "utf-8",
  timeout: 10_000,
});
if (npmVersion.status !== 0) {
  fail(npmVersion);
}
if (npmVersion.stdout.trim() !== REQUIRED_NPM) {
  cleanup();
  throw new Error(
    `Reference Capture Row requires npm ${REQUIRED_NPM}, received ${npmVersion.stdout.trim()}`,
  );
}
const captureCommands = [
  {
    cwd: "packages/agent",
    tests: ["test/agent-loop.test.ts"],
  },
  {
    cwd: "packages/coding-agent",
    tests: [
      "test/print-mode.test.ts",
      "test/omh-reference-session-scoped-extension-modules.test.ts",
    ],
  },
];
const install = runIsolated("npm", ["ci", "--offline"], {
  cwd: referenceExport,
  env: cleanEnvironment,
  encoding: null,
  maxBuffer: 16 * 1024 * 1024,
  timeout: 120_000,
});
if (install.status !== 0) {
  fail(install);
}
const networkGuard = fileURLToPath(
  new URL("./reference_network_guard.cjs", import.meta.url),
);
for (const config of [
  "packages/tui/tsconfig.build.json",
  "packages/ai/tsconfig.build.json",
  "packages/agent/tsconfig.build.json",
]) {
  const build = runIsolated(
    join(referenceExport, "node_modules/.bin/tsgo"),
    ["-p", config],
    {
      cwd: referenceExport,
      env: {
        ...cleanEnvironment,
        NODE_OPTIONS: `--require=${networkGuard}`,
      },
      encoding: null,
      maxBuffer: 16 * 1024 * 1024,
      timeout: 60_000,
    },
  );
  if (build.status !== 0) {
    fail(build);
  }
}
copyFileSync(captureTest, installedCaptureTest);
for (const command of captureCommands) {
  const capture = runIsolated("npm", ["test", "--", ...command.tests], {
    cwd: `${referenceExport}/${command.cwd}`,
    env: {
      ...cleanEnvironment,
      NODE_OPTIONS: `--require=${networkGuard}`,
      OMH_REFERENCE_CAPTURE_OUTPUT: captureOutput,
    },
    encoding: null,
    maxBuffer: 16 * 1024 * 1024,
    timeout: 120_000,
  });
  if (capture.status !== 0) {
    fail(capture);
  }
}

const generator = fileURLToPath(
  new URL("./regenerate_reference_corpus.py", import.meta.url),
);
const sourceCaptureDriver = fileURLToPath(
  new URL("./capture_reference_source_cases.py", import.meta.url),
);
const sourceCapture = runIsolated(
  python,
  [sourceCaptureDriver, manifest, "--reference-export", referenceExport],
  {
    env: cleanEnvironment,
    encoding: null,
    maxBuffer: 16 * 1024 * 1024,
    timeout: 10_000,
  },
);
if (sourceCapture.status !== 0) {
  fail(sourceCapture);
}
const captureLines = readFileSync(captureOutput, "utf-8")
  .split("\n")
  .filter(Boolean);
if (captureLines.length !== 1) {
  cleanup();
  throw new Error("Reference runtime capture produced an unexpected case set");
}
const captureDocuments = [
  JSON.parse(sourceCapture.stdout.toString("utf-8")),
  ...captureLines.map((line) => JSON.parse(line)),
];
const capturedCases = [];
for (const document of captureDocuments) {
  if (
    document.schemaVersion !== 1 ||
    document.referenceRevision !== "0e6909f050eeb15e8f6c05185511f3788357ddb3" ||
    !Array.isArray(document.cases)
  ) {
    cleanup();
    throw new Error("Reference capture driver returned an invalid envelope");
  }
  capturedCases.push(...document.cases);
}
const normalizedCapture = join(captureRoot, "runtime-capture.json");
writeFileSync(
  normalizedCapture,
  `${JSON.stringify({
    schemaVersion: 1,
    referenceRevision: "0e6909f050eeb15e8f6c05185511f3788357ddb3",
    cases: capturedCases,
  })}\n`,
  "utf-8",
);
const completed = runIsolated(
  python,
  [
    generator,
    manifest,
    "--reference-export",
    referenceExport,
    "--capture-output",
    normalizedCapture,
  ],
  {
    env: cleanEnvironment,
    encoding: null,
    maxBuffer: 16 * 1024 * 1024,
    timeout: 10_000,
  },
);
if (completed.status !== 0) {
  fail(completed);
}
process.stdout.write(completed.stdout);
cleanup();
