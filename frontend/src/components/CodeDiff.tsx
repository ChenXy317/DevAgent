export type DiffLine = {
  kind: "same" | "add" | "del";
  text: string;
};

export function diffLines(before: string, after: string): DiffLine[] {
  const oldLines = before.split("\n");
  const newLines = after.split("\n");
  const scores = Array.from({ length: oldLines.length + 1 }, () =>
    Array<number>(newLines.length + 1).fill(0),
  );
  for (let i = oldLines.length - 1; i >= 0; i -= 1) {
    for (let j = newLines.length - 1; j >= 0; j -= 1) {
      scores[i][j] =
        oldLines[i] === newLines[j]
          ? scores[i + 1][j + 1] + 1
          : Math.max(scores[i + 1][j], scores[i][j + 1]);
    }
  }
  const lines: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < oldLines.length && j < newLines.length) {
    if (oldLines[i] === newLines[j]) {
      lines.push({ kind: "same", text: oldLines[i] });
      i += 1;
      j += 1;
    } else if (scores[i + 1][j] >= scores[i][j + 1]) {
      lines.push({ kind: "del", text: oldLines[i] });
      i += 1;
    } else {
      lines.push({ kind: "add", text: newLines[j] });
      j += 1;
    }
  }
  while (i < oldLines.length) {
    lines.push({ kind: "del", text: oldLines[i] });
    i += 1;
  }
  while (j < newLines.length) {
    lines.push({ kind: "add", text: newLines[j] });
    j += 1;
  }
  return lines;
}

export function CodeDiff({ before, after }: { before: string; after: string }) {
  const lines = diffLines(before, after);
  return (
    <pre className="diff">
      {lines.map((line, index) => (
        <div key={`${line.kind}-${index}`} className={`diff-line ${line.kind}`}>
          <span>{line.kind === "add" ? "+" : line.kind === "del" ? "-" : " "}</span>
          {line.text || " "}
        </div>
      ))}
    </pre>
  );
}
