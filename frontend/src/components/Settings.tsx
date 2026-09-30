import { useEffect, useState } from "react";

import {
  createRule,
  deleteRule,
  listRules,
  probeLlm,
  readLlm,
  saveLlm,
  type AllowRule,
} from "../api/rest";
import { refreshLlm } from "../store/agentStore";
import { setAccent, setTheme, useTheme, type AccentName, type ThemeName } from "../store/themeStore";

const ACCENTS: { id: AccentName; label: string }[] = [
  { id: "cyan", label: "青" },
  { id: "green", label: "绿" },
  { id: "purple", label: "紫" },
  { id: "amber", label: "琥珀" },
];

const KINDS = [
  ["shell_prefix", "命令"],
  ["write_path", "写入路径"],
  ["edit_path", "修改路径"],
] as const;

export function Settings({ onClose }: { onClose: () => void }) {
  const theme = useTheme();
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [turns, setTurns] = useState("20");
  const [rules, setRules] = useState<AllowRule[]>([]);
  const [kind, setKind] = useState<(typeof KINDS)[number][0]>("shell_prefix");
  const [pattern, setPattern] = useState("");
  const [modelNotice, setModelNotice] = useState("");
  const [ruleNotice, setRuleNotice] = useState("");
  const [saving, setSaving] = useState(false);
  const [probing, setProbing] = useState(false);
  const [ruleBusy, setRuleBusy] = useState(false);

  useEffect(() => {
    let active = true;
    Promise.all([readLlm(), listRules()])
      .then(([llm, nextRules]) => {
        if (!active) {
          return;
        }
        setBaseUrl(llm.base_url);
        setModel(llm.model);
        setTurns(String(llm.context_turns));
        setRules(nextRules);
        refreshLlm(llm);
      })
      .catch((error: unknown) => {
        if (active) {
          setModelNotice(error instanceof Error ? error.message : "加载失败");
        }
      });
    return () => {
      active = false;
    };
  }, []);

  async function onSave() {
    if (!baseUrl.trim()) {
      setModelNotice("请填写模型地址");
      return;
    }
    if (!model.trim()) {
      setModelNotice("请填写模型名");
      return;
    }
    const contextTurns = Number(turns);
    if (!Number.isInteger(contextTurns) || contextTurns < 1 || contextTurns > 200) {
      setModelNotice("轮数需要在 1 到 200 之间");
      return;
    }
    setSaving(true);
    setModelNotice("");
    try {
      const saved = await saveLlm({
        base_url: baseUrl.trim(),
        model: model.trim(),
        api_key: apiKey,
        context_turns: contextTurns,
      });
      refreshLlm(saved);
      setApiKey("");
      setModelNotice(apiKey.trim() ? "已保存" : "已保存。密钥未改");
    } catch (error) {
      setModelNotice(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function onProbe() {
    setProbing(true);
    setModelNotice("");
    try {
      const result = await probeLlm({
        base_url: baseUrl.trim(),
        model: model.trim(),
        api_key: apiKey,
      });
      setModelNotice(result.ok ? "连接成功" : result.error || "连接失败");
    } catch (error) {
      setModelNotice(error instanceof Error ? error.message : "连接失败");
    } finally {
      setProbing(false);
    }
  }

  async function onAddRule() {
    if (!pattern.trim()) {
      setRuleNotice("请填写前缀");
      return;
    }
    setRuleBusy(true);
    setRuleNotice("");
    try {
      const created = await createRule(kind, pattern);
      setRules((current) => [...current, created]);
      setPattern("");
    } catch (error) {
      setRuleNotice(error instanceof Error ? error.message : "添加失败");
    } finally {
      setRuleBusy(false);
    }
  }

  async function onDeleteRule(id: number) {
    setRuleBusy(true);
    setRuleNotice("");
    try {
      await deleteRule(id);
      setRules((current) => current.filter((rule) => rule.id !== id));
    } catch (error) {
      setRuleNotice(error instanceof Error ? error.message : "删除失败");
    } finally {
      setRuleBusy(false);
    }
  }

  return (
    <div className="settings">
      <header className="topbar">
        <strong>设置</strong>
        <button type="button" onClick={onClose}>
          关闭
        </button>
      </header>
      <div className="settings-body">
        <section>
          <h2>外观</h2>
          <p className="muted">当前是{theme.theme === "dark" ? "暗色" : "亮色"}，{labelOf(theme.accent)}</p>
          <div className="row">
            {(["dark", "light"] as ThemeName[]).map((name) => (
              <button key={name} type="button" onClick={() => setTheme(name)} disabled={theme.theme === name}>
                {name === "dark" ? "暗色" : "亮色"}
              </button>
            ))}
          </div>
          <div className="row">
            {ACCENTS.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => setAccent(item.id)}
                disabled={theme.accent === item.id}
              >
                {item.label}
              </button>
            ))}
          </div>
        </section>
        <section>
          <h2>放行规则</h2>
          <div className="rule-form">
            <select value={kind} onChange={(event) => setKind(event.target.value as typeof kind)}>
              {KINDS.map(([id, label]) => (
                <option key={id} value={id}>
                  {label}
                </option>
              ))}
            </select>
            <input value={pattern} onChange={(event) => setPattern(event.target.value)} placeholder="前缀" />
            <button type="button" onClick={onAddRule} disabled={ruleBusy}>
              {ruleBusy ? "处理中" : "添加"}
            </button>
          </div>
          {ruleNotice ? <p className="notice">{ruleNotice}</p> : null}
          <ul className="rules">
            {rules.map((rule) => (
              <li key={rule.id}>
                <span>{kindLabel(rule.kind)}</span>
                <code>{rule.pattern}</code>
                <button type="button" onClick={() => onDeleteRule(rule.id)} disabled={ruleBusy}>
                  删除
                </button>
              </li>
            ))}
          </ul>
        </section>
        <section>
          <h2>模型</h2>
          <label>
            地址
            <input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} />
          </label>
          <label>
            模型名
            <input value={model} onChange={(event) => setModel(event.target.value)} />
          </label>
          <label>
            密钥
            <input
              type="password"
              value={apiKey}
              placeholder="留空则不修改"
              onChange={(event) => setApiKey(event.target.value)}
            />
          </label>
          <label>
            保留轮数
            <input value={turns} onChange={(event) => setTurns(event.target.value)} />
          </label>
          {modelNotice ? <p className="notice">{modelNotice}</p> : null}
          <p className="muted">测试当前填写的地址和模型。密钥留空时使用已保存的密钥。</p>
          <div className="row">
            <button type="button" onClick={onSave} disabled={saving}>
              {saving ? "保存中" : "保存"}
            </button>
            <button type="button" onClick={onProbe} disabled={probing}>
              {probing ? "测试中" : "测试连接"}
            </button>
          </div>
        </section>
      </div>
    </div>
  );
}

function labelOf(accent: AccentName) {
  return ACCENTS.find((item) => item.id === accent)?.label ?? accent;
}

function kindLabel(kind: string) {
  return KINDS.find((item) => item[0] === kind)?.[1] ?? kind;
}
