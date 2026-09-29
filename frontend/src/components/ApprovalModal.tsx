import { useEffect, useState } from "react";

import type { ApprovalRequest } from "../api/ws";

const DECISIONS = [
  ["approve", "本次允许"],
  ["approve_always", "以后都允许"],
  ["reject", "拒绝"],
] as const;

export function ApprovalModal({
  request,
  onDecide,
}: {
  request: ApprovalRequest;
  onDecide: (decision: (typeof DECISIONS)[number][0]) => void;
}) {
  const [locked, setLocked] = useState(false);
  const detail =
    typeof request.arguments === "string"
      ? request.arguments
      : JSON.stringify(request.arguments ?? {}, null, 2);

  useEffect(() => {
    setLocked(false);
  }, [request.id]);

  return (
    <div className="approval">
      <p>
        等待审批：{request.tool || "工具"}
        {request.prefix ? `。以后都允许将保存前缀 ${request.prefix}` : ""}
      </p>
      <pre>{detail}</pre>
      <div className="row">
        {DECISIONS.map(([decision, label]) => (
          <button
            key={decision}
            type="button"
            disabled={locked}
            onClick={() => {
              setLocked(true);
              onDecide(decision);
            }}
          >
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}
