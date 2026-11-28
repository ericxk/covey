/* 话题页（样张 A）用的纯函数：把轮次分成“你问 → 结论”，取结论第一段、第一句，概括主人那句话。 */
import type { SnapshotTopic, TopicProgress, TurnAnswer, TurnFailure, TurnView } from '@shared/api.interface';
import { isRunningTone, toneOf, toneWord, waitText, type ToneKey } from './lib';

/** 一轮：主人的几句话 + Covey 的回答（还没回答时 agent 为 null）；event 行单独成一组 */
export interface Round {
  /** 第几轮（含网页上没载入的更早轮次） */
  n: number;
  owner: TurnView[];
  agent: TurnView | null;
  event?: boolean;
  /** 这一轮所有 seq，用于 ?seq= 定位 */
  seqs: number[];
}

export function groupRounds(turns: TurnView[], totalRounds: number): Round[] {
  const out: Round[] = [];
  let owner: TurnView[] = [];
  for (const t of turns) {
    // v19：主人编辑的那一行已经并进原消息那一行（Covey 给原消息改后的全文），不再单画
    if (t.role === 'owner') {
      if (!t.merged) owner.push(t);
    }
    else if (t.role === 'event') out.push({ n: 0, owner: [], agent: t, event: true, seqs: [t.seq] });
    else {
      out.push({ n: 0, owner, agent: t, seqs: [...owner.map((o: TurnView) => o.seq), t.seq] });
      owner = [];
    }
  }
  if (owner.length) out.push({ n: 0, owner, agent: null, seqs: owner.map((o: TurnView) => o.seq) });
  const answered: number = out.filter((r: Round) => !r.event && r.agent).length;
  // 网页只载入最近 200 条：编号从话题的总轮数往回数
  let n: number = Math.max(0, (totalRounds || 0) - answered);
  for (const r of out) {
    if (r.event) continue;
    if (r.agent) n += 1;
    r.n = r.agent ? n : n + 1;
  }
  return out;
}

// 词里的下划线（frontier_llm、toolu_01…）不是记号，留着
const MD_NOISE = /[*`~>#|]+|(?<![\p{L}\p{N}])_+|_+(?![\p{L}\p{N}])/gu;
const flat = (s: string): string => s.replace(/\s+/gu, ' ').trim();

/** Markdown 的第一段正文：跳过标题、代码块、表格、分隔线，去掉记号 */
export function firstParagraph(md: string, max = 220): string {
  const text: string = (md || '').replace(/\r\n/gu, '\n').replace(/```[\s\S]*?(```|$)/gu, '\n\n');
  for (const block of text.split(/\n\s*\n/u)) {
    const lines: string[] = block.split('\n').map((l: string) => l.trim()).filter(Boolean);
    if (!lines.length) continue;
    if (lines.every((l: string) => /^(#{1,6}\s|\|.*\||[-*_]{3,}$)/u.test(l))) continue;
    const body: string[] = lines.filter((l: string) => !/^#{1,6}\s/u.test(l) && !/^\|.*\|$/u.test(l));
    const s: string = flat(body.join(' ').replace(/!?\[([^\]]*)\]\([^)]*\)/gu, '$1').replace(MD_NOISE, ' ').replace(/^\s*[-+]\s+/u, ''));
    if (!s) continue;
    return s.length > max ? `${s.slice(0, max)}…` : s;
  }
  return '';
}

/** 第一句：到第一个句号/问号/感叹号为止（含），没有就整段 */
export function firstSentence(md: string, max = 120): string {
  const p: string = firstParagraph(md, 400);
  const m: RegExpMatchArray | null = p.match(/^.+?[。！？!?](?=\s|$|[^。！？!?])/u);
  const s: string = m ? m[0] : p;
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

/** v7：一轮给人看的正文（Covey 去掉了内部前缀），旧数据没有 display 时是原文 */
export const shownText = (t: TurnView): string => t.display ?? t.body;

/** v7：谁说的那几个字：你、负责人、管家（专项负责人、管家转来你的原话仍是你） */
export const BY_WORD: Record<string, string> = { owner: '你', lead: '负责人', butler: '管家' };
export const byWord = (t: TurnView | undefined): string => BY_WORD[t?.by ?? 'owner'] ?? '你';
/** 展开后那一块的标题：你说 / 负责人说 / 管家说；答题的是“你的回答 / 负责人替你答 / 管家替你答” */
export function saidLabel(t: TurnView, answer: boolean): string {
  const by: string = t.by ?? 'owner';
  if (answer) return by === 'lead' ? '负责人替你答' : by === 'butler' ? '管家替你答' : '你的回答';
  if (by === 'lead') return '负责人说';
  if (by === 'butler') return '管家说';
  return t.via === 'lead' ? '你说（负责人转来）' : t.via === 'butler' ? '你说（管家转来）' : '你说';
}

export const ANSWER_HEAD = '用户补充回答：';
/** 旧数据：主人回答问题卡那一轮的正文（没有 answers 字段时） */
export const isAnswerBody = (body: string): boolean => (body || '').trimStart().startsWith(ANSWER_HEAD) || (body || '').trimStart().startsWith('用户对当前提问的反馈');

/** 这一轮的回答（问题 → 选择）：先看 agent 轮，再看主人那几句 */
export function roundAnswers(r: Round): TurnAnswer[] | null {
  if (r.agent?.answers?.length) return r.agent.answers;
  for (const o of r.owner) if (o.answers?.length) return o.answers;
  return null;
}

const SKIP: readonly string[] = ['没答', '跳过', '不回答', ''];
/** 左边“你”那一格：主人那句话的一行摘要 */
/** 左边“你”那一格摘要的是哪一句：有回答问题卡的那句就是它，否则是最后一句（v7 说话人跟着这一句走） */
export function summaryTurn(r: Round): TurnView | undefined {
  return r.owner.find((o: TurnView) => !!o.answers?.length || isAnswerBody(o.body)) ?? r.owner[r.owner.length - 1];
}

export function ownerSummary(r: Round): string {
  const shown: TurnView | undefined = summaryTurn(r);
  if (shown?.display) {
    // v7：Covey 已经写好给人看的一句（“答了 2 题：B；没答”“没答，补充：…”“撤回了…”）
    const s: string = flat(shown.display.replace(MD_NOISE, ' '));
    return s.length > 160 ? `${s.slice(0, 160)}…` : s;
  }
  const ans: TurnAnswer[] | null = roundAnswers(r);
  if (ans) {
    const given: string[] = ans.map((a: TurnAnswer) => a.choice).filter((c: string) => !SKIP.includes(c.trim()));
    return `答了 ${ans.length} 题${given.length ? `：${given.join('；')}` : '（都没答）'}`;
  }
  const said: string[] = r.owner.map(shownText).filter((b: string) => b.trim());
  if (!said.length) return '';
  const last: string = said[said.length - 1];
  if (isAnswerBody(last)) {
    // 旧数据只取个大概：“用户补充回答：”之后，问题和回答各占一行
    const lines: string[] = last.split('\n\n')[0].split('\n').slice(1).map((l: string) => l.trim()).filter(Boolean);
    const picks: string[] = lines.length % 2 === 0 ? lines.filter((_: string, i: number) => i % 2 === 1) : [];
    return picks.length ? `答了 ${picks.length} 题：${picks.join('；')}` : '回答了问题卡';
  }
  const s: string = flat(said.join(' ').replace(MD_NOISE, ' '));
  return s.length > 160 ? `${s.slice(0, 160)}…` : s;
}

/** 右边“结论”那一格（不含失败，失败另画） */
export function conclusion(r: Round): string {
  const a: TurnView | null = r.agent;
  if (!a) return '';
  return firstSentence(a.body);
}

export const isFailed = (t: TurnView | null | undefined): boolean => t?.status === 'failed';

/** v31：协助方答咨询的那几行（collab.role = helper）不是这个话题自己的工作：最近一轮、最新结论都不算它们 */
export const isHelperTurn = (t: TurnView): boolean => t.collab?.role === 'helper';

/** 话题最新的一轮回答（不含 event、不含答咨询的那一轮） */
export function lastAgent(turns: TurnView[]): TurnView | undefined {
  for (let i = turns.length - 1; i >= 0; i -= 1) if (turns[i].role !== 'owner' && turns[i].role !== 'event' && !isHelperTurn(turns[i])) return turns[i];
  return undefined;
}

/** 最新结论：最后一个做完、有正文的回答 */
export function lastDoneAgent(turns: TurnView[]): TurnView | undefined {
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    const t: TurnView = turns[i];
    if (t.role !== 'owner' && t.role !== 'event' && !isHelperTurn(t) && t.status === 'done' && t.body.trim()) return t;
  }
  return undefined;
}

/* 没成功的一轮的按钮怎么叫、怎么做在 recover.ts（v33：照 failure.labels、how 发 recover，不再替你说一句话） */

/** 没有 failure 字段的失败轮：只给「检查并继续」（发 recover，Covey 按记录定能不能做） */
export const FALLBACK_FAILURE: TurnFailure = { title: '上一轮没做完', actions: ['continue'] };

/** 在做的话题此刻的进度说明（T647 P1-C4，同飞书任务卡的状态行；docs/web.md 进度 slow_step、command）：
 * - 在等一条命令跑完：“在等一条命令跑完 · 已 38 分钟”（仍是在做，不算卡住）；
 * - 这步比平均慢：“第 3 步已做 35 分钟，此前平均每步约 6 分钟”；
 * - 可能卡住：“40 分钟没有动静，可能卡住了”（分钟数从最后动静算，不写死 30 分钟：阈值是 Covey 的 COVEY_PROGRESS_STUCK_MINUTES）。
 * 都没有是空串。stuck：这句说的是可能卡住（橙色） */
export function progressNote(p: TopicProgress | undefined, now: number): { text: string; stuck: boolean } {
  if (!p) return { text: '', stuck: false };
  const since = (at: number | null | undefined, secs: number): number => (at ? Math.max(secs, now - at) : secs);
  if (p.command) return { text: `在等一条命令跑完 · 已 ${waitText(since(p.command.since, p.command.seconds))}`, stuck: false };
  if (p.maybe_stuck) {
    const quiet: string = p.last_activity_at ? `${waitText(now - p.last_activity_at)}没有动静，` : '';
    return { text: `${quiet}可能卡住了`, stuck: true };
  }
  if (p.slow_step) {
    const s = p.slow_step;
    const usual: string = s.usual ? `，此前平均每步约 ${waitText(s.usual)}` : '';
    return { text: `第 ${s.number} 步已做 ${waitText(since(s.since, s.seconds))}${usual}`, stuck: false };
  }
  return { text: '', stuck: false };
}

/** 页面上的状态字：look 的词表（等你、停住了、可能卡住、有结果、在做……） */
export function stateWord(t: Pick<SnapshotTopic, 'state' | 'unseen' | 'tone'>): string {
  return toneWord(toneOf(t));
}

/** “3 分钟前”“2 小时前” */
export function ago(sec: number | null | undefined, now: number): string {
  if (!sec) return '';
  const d: number = Math.max(0, now - sec);
  if (d < 60) return '刚刚';
  if (d < 3600) return `${Math.round(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return `${Math.floor(d / 86400)} 天前`;
}

/** 预计还要多久：秒 → “约 1 小时 30 分”“约 40 分” */
export function etaText(sec: number | null | undefined): string {
  if (sec === null || sec === undefined || !Number.isFinite(sec)) return '';
  const m: number = Math.max(1, Math.round(sec / 60));
  if (m < 60) return `约 ${m} 分`;
  const h: number = Math.floor(m / 60);
  const r: number = m % 60;
  return r ? `约 ${h} 小时 ${r} 分` : `约 ${h} 小时`;
}

/** 列表里的颜色：停住了的话题不在看板上了（看过 12 小时、点了“不管了”），就和已停止一样灰着，不再是红点 */
export function listTone(t: SnapshotTopic): ToneKey {
  const k: ToneKey = toneOf(t);
  return k === 'stopped' && typeof t.board === 'string' && t.board !== 'stopped' ? 'cancelled' : k;
}

/** 停住：最新一轮没做完、话题不在做也不在等你。开着看板时跟看板分组走（点了「不管了」、看过 12 小时的不算，和飞书一个数）；
 * 没开看板的旧数据取三天内的（更早的不再占要你处理） */
const STOP_WINDOW = 3 * 86400;
export function isStopped(t: SnapshotTopic, now: number): boolean {
  if (t.kind !== 'topic') return false;
  if (typeof t.board === 'string') return t.board === 'stopped';
  const k = toneOf(t);
  if (isRunningTone(k) || k === 'waiting') return false;
  return (k === 'stopped' || t.latest_status === 'failed') && now - t.updated_at < STOP_WINDOW;
}

/** failure 的那句话拆成（原因，能做什么）：第一个“；”前是原因，超过 40 字的原因在 40 字前最后一个“，”再拆（同 core/failure.py split_title） */
const HEADLINE_CHARS = 40;
export function splitTitle(title: string): [string, string] {
  const s: string = title || '';
  const at: number = s.indexOf('；');
  let head: string = at >= 0 ? s.slice(0, at) : s;
  let rest: string = at >= 0 ? s.slice(at + 1) : '';
  if ([...head].length > HEADLINE_CHARS) {
    const cut: number = head.lastIndexOf('，', HEADLINE_CHARS);
    if (cut > 0) {
      rest = head.slice(cut + 1) + (rest ? `；${rest}` : '');
      head = head.slice(0, cut);
    }
  }
  return [head, rest];
}

/** 停住的话题还算不算“要你处理”：跟看板分组走（v6 board）；没有 board 的旧数据照旧算 */
export function stopPressing(t: Pick<SnapshotTopic, 'board'>): boolean {
  return typeof t.board === 'string' ? t.board === 'stopped' : true;
}

/** “等你 · 要你定”那条带子的两行：话题页的总览和专项页负责人对话的顶上（T952）是同一条，一处写 */
export function askBand(q: { questions: { question?: string }[] }): { label: string; head: string } {
  const n: number = q.questions.length;
  return { label: `等你 · 要你定${n > 1 ? `（共 ${n} 题）` : ''}`, head: q.questions[0]?.question || '在等你的回答' };
}
