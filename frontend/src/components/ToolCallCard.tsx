import { CodeDiff } from "./CodeDiff";

type ToolCallCardProps = {
  name: string;
  detail: string;
  status?: string;
  error?: string;
  output?: string;
  before?: string;
  after?: string;
};

function safeHttp(url: string) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}

function SearchList({ text }: { text: string }) {
  try {
    const body = JSON.parse(text) as { results?: { title?: string; url?: string }[] };
    if (!Array.isArray(body.results)) {
      return null;
    }
    return (
      <ul className="search-list">
        {body.results.map((item) => (
          <li key={item.url || item.title}>
            {item.url && safeHttp(item.url) ? (
              <a href={item.url} target="_blank" rel="noopener noreferrer">
                {item.title || item.url}
              </a>
            ) : (
              item.title || "无标题"
            )}
          </li>
        ))}
      </ul>
    );
  } catch {
    return null;
  }
}

export function ToolCallCard({ name, detail, status, error, output, before, after }: ToolCallCardProps) {
  const links = name === "web_search" && output ? <SearchList text={output} /> : null;
  return (
    <article className="tool-card">
      <header>
        <strong>{name}</strong>
        {status ? <span>{status}</span> : null}
      </header>
      {detail ? <pre>{detail}</pre> : null}
      {links || (output ? <pre>{output}</pre> : null)}
      {before !== undefined && after !== undefined ? <CodeDiff before={before} after={after} /> : null}
      {error ? <p className="tool-error">{error}</p> : null}
    </article>
  );
}
