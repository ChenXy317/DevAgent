export function ThinkingBlock({ text }: { text: string }) {
  if (!text.trim()) {
    return null;
  }
  return (
    <details className="thinking">
      <summary>思考</summary>
      <pre>{text}</pre>
    </details>
  );
}
