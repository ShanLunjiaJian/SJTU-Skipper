import { mkdir, realpath, copyFile, readdir, lstat } from "node:fs/promises";
import path from "node:path";
import { editInspect } from "@oh-my-pi/pi-natives";
import {
  splitPathAndSelPreferringLiteralSync,
  splitDelimitedPathEntry,
} from "@oh-my-pi/pi-coding-agent/tools/path-utils";
type EditMode = Parameters<typeof editInspect>[0];

export class Workspace {
  readonly files = new Map<string, string>();
  readonly writable = new Set<string>();
  readonly published = new Map<string, string>();
  constructor(readonly cwd: string) {}

  async initialize(files: Record<string, string>, readonly: string[]) {
    for (const [logical, source] of Object.entries(files)) {
      const destination = path.resolve(this.cwd, logical);
      if (!destination.startsWith(this.cwd + path.sep))
        throw new Error(`Invalid workspace path: ${logical}`);
      await mkdir(path.dirname(destination), { recursive: true });
      const original = await realpath(source);
      if (original !== destination) await copyFile(original, destination);
      this.files.set(destination, original);
      this.published.set(destination, await Bun.file(destination).text());
      if (!readonly.includes(logical)) this.writable.add(destination);
    }
  }

  async check(name: string, args: Record<string, unknown>, mode: EditMode) {
    const paths: string[] = [];
    const writing = name === "edit" || name === "write";
    if (name === "edit") {
      const inspection = editInspect(mode, JSON.stringify(args));
      paths.push(...inspection.paths);
      for (const operation of inspection.fileOps)
        if (operation.kind === "move" && operation.to) paths.push(operation.to);
      if (!paths.length) throw new Error("Edit contains no authorized target");
    } else paths.push(String(args.path ?? "."));
    for (const raw of paths) {
      const entries = await splitDelimitedPathEntry(raw, this.cwd);
      for (const entry of entries ?? [raw]) {
        const selected = entry.trim();
        if (selected.includes("://") || path.isAbsolute(selected))
          throw new Error(`Unauthorized path: ${selected}`);
        const target = splitPathAndSelPreferringLiteralSync(selected, this.cwd).path;
        const absolute = path.resolve(this.cwd, target);
        if (absolute !== this.cwd && !absolute.startsWith(this.cwd + path.sep))
          throw new Error(`Path leaves workspace: ${raw}`);
        if (writing && !this.writable.has(absolute))
          throw new Error(`Read-only or unknown target: ${raw}`);
        if (name === "read" && !this.files.has(absolute) && absolute !== this.cwd)
          throw new Error(`Unknown file: ${raw}`);
      }
    }
    await this.checkTree(this.cwd);
  }

  private async checkTree(directory: string): Promise<void> {
    for (const name of await readdir(directory)) {
      const target = path.join(directory, name);
      const stat = await lstat(target);
      if (stat.isSymbolicLink()) throw new Error(`Symbolic link is not authorized: ${name}`);
      if (stat.isDirectory()) await this.checkTree(target);
    }
  }

  async changes() {
    const changed: { file: string; path: string; content: string; previous: string }[] = [];
    for (const [target, original] of this.files) {
      if (!this.writable.has(target)) continue;
      const local = Bun.file(target);
      if (!(await local.exists())) throw new Error(`Required document was removed: ${target}`);
      const content = await local.text();
      const previous = this.published.get(target) ?? "";
      if (content !== previous) {
        changed.push({ file: path.relative(this.cwd, target), path: original, content, previous });
        this.published.set(target, content);
      }
    }
    return changed;
  }
}
