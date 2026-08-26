import {
	appendFileSync,
	mkdirSync,
	mkdtempSync,
	readFileSync,
	rmSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
	clearExtensionCache,
	loadExtensionsCached,
} from "../src/core/extensions/loader.ts";

const REFERENCE_REVISION = "0e6909f050eeb15e8f6c05185511f3788357ddb3";
const captureOutput = process.env.OMH_REFERENCE_CAPTURE_OUTPUT;

if (!captureOutput) {
	throw new Error("OMH_REFERENCE_CAPTURE_OUTPUT is required");
}

function writeCountingExtension(
	extensionPath: string,
	tracePath: string,
	version: number,
): void {
	writeFileSync(
		extensionPath,
		`import { appendFileSync } from "node:fs";
appendFileSync(${JSON.stringify(tracePath)}, "M${version}\\n", "utf-8");

export default function () {
	appendFileSync(${JSON.stringify(tracePath)}, "F${version}\\n", "utf-8");
}
`,
		"utf-8",
	);
}

describe("omh fixed Reference capture", () => {
	const roots: string[] = [];

	afterEach(() => {
		clearExtensionCache();
		while (roots.length > 0) {
			const root = roots.pop();
			if (root) {
				rmSync(root, { recursive: true, force: true });
			}
		}
	});

	it("captures session-scoped extension module behavior", async () => {
		const root = mkdtempSync(join(tmpdir(), "omh-reference-extension-"));
		roots.push(root);
		const cwd = join(root, "project");
		const extensionPath = join(root, "counting.ts");
		const tracePath = join(root, "trace.txt");
		mkdirSync(cwd);
		writeFileSync(tracePath, "", "utf-8");
		writeCountingExtension(extensionPath, tracePath, 1);

		const first = await loadExtensionsCached([extensionPath], cwd);
		const second = await loadExtensionsCached([extensionPath], cwd);
		expect(first.errors).toEqual([]);
		expect(second.errors).toEqual([]);
		expect(readFileSync(tracePath, "utf-8").split("\n").filter(Boolean)).toEqual([
			"M1",
			"F1",
			"F1",
		]);

		writeCountingExtension(extensionPath, tracePath, 2);
		const live = await loadExtensionsCached([extensionPath], cwd);
		expect(live.errors).toEqual([]);
		expect(readFileSync(tracePath, "utf-8").split("\n").filter(Boolean)).toEqual([
			"M1",
			"F1",
			"F1",
			"F1",
		]);

		clearExtensionCache();
		const reloaded = await loadExtensionsCached([extensionPath], cwd);
		expect(reloaded.errors).toEqual([]);
		expect(readFileSync(tracePath, "utf-8").split("\n").filter(Boolean)).toEqual([
			"M1",
			"F1",
			"F1",
			"F1",
			"M2",
			"F2",
		]);

		appendFileSync(
			captureOutput,
			`${JSON.stringify({
				schemaVersion: 1,
				referenceRevision: REFERENCE_REVISION,
				cases: [
					{
						id: "reference.session-scoped-extension-modules",
						observations: {
							A: "cached factory reuse",
							L: [],
							T: "one module load, repeated factory",
							E: "shared module cache",
							C: "in-place reload possible",
						},
					},
				],
			})}\n`,
			"utf-8",
		);
	});
});
