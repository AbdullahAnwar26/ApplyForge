// Runs Vitest from a working directory with an upper-case Windows drive letter.
// Some terminals (e.g. VS Code's) report "c:\..."; Vitest then loads itself twice
// and every suite fails with "Vitest failed to find the runner".
import { spawnSync } from "node:child_process";
import { join } from "node:path";

const cwd = process.cwd().replace(/^[a-z]:/, (drive) => drive.toUpperCase());
const bin = join(cwd, "node_modules", "vitest", "vitest.mjs");
const result = spawnSync(process.execPath, [bin, ...process.argv.slice(2)], { cwd, stdio: "inherit" });
process.exit(result.status ?? 1);
