import { BadRequestException, Body, Controller, ForbiddenException, Get, NotFoundException, Param, Post, Query, Req, Res } from '@nestjs/common';
import type { Request, Response } from 'express';
import { NeedLogin } from '@lark-apaas/fullstack-nestjs-core';
import type {
  HomeResponse,
  LocateResponse,
  OutboxRequest,
  OutboxResponse,
  OutboxStatus,
  ProjectDetailResponse,
  ReportResponse,
  SearchHit,
  SettingsResponse,
  TidyResponse,
  StatsResponse,
  UsageResponse,
  SnapshotResponse,
  TurnsResponse,
  RoomResponse,
  TopicDetailResponse,
  UploadDoneResponse,
  UploadPartRequest,
  UploadStartRequest,
  UploadStartResponse,
  MainMemoryResponse,
  UpgradeResponse,
  QuotaResponse,
  MemoryKeyResponse,
} from '@shared/api.interface';
import { MEMORY_KEY_RE } from '@shared/api.interface';
import { WorkbenchImagesService } from './workbench.images.service';
import { WorkbenchQueryService } from './workbench.query.service';
import { WorkbenchUploadsService } from './workbench.uploads.service';
import { waitSeconds } from './workbench.wake';

/** 页面问回执最多挂多久（秒） */
const OUTBOX_WAIT_MAX = 8;

/** 主人的妙搭 user_id 放环境变量 COVEY_OWNER_USER_ID；没配就谁都不放行 */
function mustBeOwner(req: Request): void {
  const owner: string = process.env.COVEY_OWNER_USER_ID ?? '';
  const uid: string = String(req.userContext?.userId ?? '');
  if (!owner || uid !== owner) throw new ForbiddenException('只有主人能用');
}

/** 把服务端耗时放进 Server-Timing，浏览器的 performance 条目里能读到 */
async function timed<T>(res: Response, fn: () => Promise<T>): Promise<T> {
  const t0: bigint = process.hrtime.bigint();
  try {
    return await fn();
  } finally {
    res.setHeader('Server-Timing', `app;dur=${(Number(process.hrtime.bigint() - t0) / 1e6).toFixed(1)}`);
  }
}

@Controller('api')
@NeedLogin()
export class WorkbenchController {
  constructor(
    private readonly svc: WorkbenchQueryService,
    private readonly images: WorkbenchImagesService,
    private readonly uploads: WorkbenchUploadsService,
  ) {}

  /** 当前登录的人：页面左下角头像用。没有名字就是 null，页面画中性图标 */
  @Get('me')
  me(@Req() req: Request): { name: string | null } {
    mustBeOwner(req);
    const name: string = String(req.userContext?.userName ?? '').trim();
    return { name: name || null };
  }

  /** v7：设置页的快照；Covey 还没推过就是 null */
  @Get('settings')
  settings(@Req() req: Request): Promise<SettingsResponse | null> {
    mustBeOwner(req);
    return this.svc.settings();
  }

  /** v37：主记忆（全局和各工作区的原则、知识条目、候选）；Covey 还没推过就是 null。
   *  v43：带 ?key=memory:index（或 MEMORY_KEY_RE 认的别的 memory:* 键）取记忆中心那一键，没推过是 null；不认的键 400 */
  @Get('memory')
  memory(@Req() req: Request, @Query('key') key?: string): Promise<MainMemoryResponse | MemoryKeyResponse | null> {
    mustBeOwner(req);
    if (key === undefined || key === '') return this.svc.memory();
    const k: string = String(key);
    if (!MEMORY_KEY_RE.test(k)) throw new BadRequestException('key 要是 memory:* 记忆键（memory:index、memory:global、memory:p:<专项 id>…）');
    return this.svc.memoryKey(k);
  }

  /** v39：版本与更新（现网和最新的 release、更新内容、上次升级的结果）；Covey 没推过就是 null */
  @Get('upgrade')
  upgrade(@Req() req: Request): Promise<UpgradeResponse | null> {
    mustBeOwner(req);
    return this.svc.upgrade();
  }

  /** v42：账号与额度（每个来源的号、两个窗口的剩余和时刻）；Covey 没推过就是 null */
  @Get('quota')
  quota(@Req() req: Request): Promise<QuotaResponse | null> {
    mustBeOwner(req);
    return this.svc.quota();
  }

  /** v16：整理专项的建议；Covey 还没推过就是 null */
  @Get('tidy')
  tidy(@Req() req: Request): Promise<TidyResponse | null> {
    mustBeOwner(req);
    return this.svc.tidy();
  }

  @Get('snapshot')
  snapshot(@Req() req: Request, @Res({ passthrough: true }) res: Response, @Query('since') since?: string): Promise<SnapshotResponse> {
    mustBeOwner(req);
    return timed(res, () => this.svc.snapshot(since ? String(since) : null));
  }

  @Get('topics/:key/turns')
  turns(
    @Req() req: Request,
    @Res({ passthrough: true }) res: Response,
    @Param('key') key: string,
    @Query('after') after?: string,
    @Query('since') since?: string,
  ): Promise<TurnsResponse> {
    mustBeOwner(req);
    const n: number = after === undefined || after === '' ? NaN : Number(after);
    return timed(res, () => this.svc.turns(key, Number.isFinite(n) ? n : null, since ? String(since) : null));
  }

  @Get('room')
  room(@Req() req: Request, @Res({ passthrough: true }) res: Response, @Query('after') after?: string): Promise<RoomResponse> {
    mustBeOwner(req);
    return timed(res, () => this.svc.room(after ? String(after) : null));
  }

  @Get('stats')
  stats(@Req() req: Request, @Res({ passthrough: true }) res: Response): Promise<StatsResponse> {
    mustBeOwner(req);
    return timed(res, () => this.svc.stats());
  }

  /** v26：用量页的按天用量；Covey 还没推过是 null */
  @Get('usage')
  usage(@Req() req: Request): Promise<UsageResponse | null> {
    mustBeOwner(req);
    return this.svc.usage();
  }

  @Get('projects/:key/report')
  report(@Req() req: Request, @Res({ passthrough: true }) res: Response, @Param('key') key: string): Promise<ReportResponse> {
    mustBeOwner(req);
    return timed(res, () => this.svc.report(key));
  }

  @Get('search')
  search(
    @Req() req: Request,
    @Res({ passthrough: true }) res: Response,
    @Query('q') q?: string,
    @Query('limit') limit?: string,
  ): Promise<SearchHit[]> {
    mustBeOwner(req);
    return timed(res, () => this.svc.search(q ?? '', Number(limit) || 30));
  }

  /** v31：记忆的“查看原文”：那道题或那条消息在哪个话题的哪一轮 */
  @Get('locate')
  locate(@Req() req: Request, @Query('message_id') messageId?: string, @Query('question') question?: string): Promise<LocateResponse> {
    mustBeOwner(req);
    const mid: string = String(messageId ?? '').slice(0, 160);
    const q: string = String(question ?? '').slice(0, 160);
    if (!mid && !q) throw new NotFoundException('要 message_id 或 question');
    return this.svc.locate(mid, q);
  }

  @Get('home')
  home(@Req() req: Request): Promise<HomeResponse> {
    mustBeOwner(req);
    return this.svc.home();
  }

  @Get('projects/:key')
  project(@Req() req: Request, @Param('key') key: string): Promise<ProjectDetailResponse> {
    mustBeOwner(req);
    return this.svc.project(key);
  }

  @Get('topics/:key')
  topic(@Req() req: Request, @Param('key') key: string): Promise<TopicDetailResponse> {
    mustBeOwner(req);
    return this.svc.topic(key);
  }

  /** v19：一轮的图片。只有主人；浏览器只在本机缓存（private），id 是内容哈希所以不变（immutable）；没有或过期了 404 */
  @Get('images/:id')
  async image(@Req() req: Request, @Res() res: Response, @Param('id') id: string, @Query('v') v?: string): Promise<void> {
    mustBeOwner(req);
    const found = await this.images.get(id, v === 'full' ? 'full' : 'thumb');
    if (!found) throw new NotFoundException('图片已过期或不存在');
    res.setHeader('Content-Type', found.mime);
    res.setHeader('Content-Length', String(found.data.length));
    res.setHeader('Cache-Control', 'private, max-age=604800, immutable');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Content-Security-Policy', "default-src 'none'; sandbox");
    res.setHeader('Content-Disposition', 'inline');
    res.end(found.data);
  }

  /** v20：输入框附件，先开一个上传 */
  @Post('uploads')
  uploadStart(@Req() req: Request, @Body() body: UploadStartRequest): Promise<UploadStartResponse> {
    mustBeOwner(req);
    return this.uploads.start(body);
  }

  /** v20：传第 n 段（原始字节 ≤ 512 KiB，base64） */
  @Post('uploads/:id/parts')
  uploadPart(@Req() req: Request, @Param('id') id: string, @Body() body: UploadPartRequest): Promise<{ ok: true }> {
    mustBeOwner(req);
    return this.uploads.part(id, body);
  }

  /** v20：段齐了，收尾 */
  @Post('uploads/:id/done')
  uploadDone(@Req() req: Request, @Param('id') id: string): Promise<UploadDoneResponse> {
    mustBeOwner(req);
    return this.uploads.done(id);
  }

  @Get('outbox/:opId')
  outboxStatus(@Req() req: Request, @Res({ passthrough: true }) res: Response, @Param('opId') opId: string, @Query('wait') wait?: string): Promise<OutboxStatus> {
    mustBeOwner(req);
    // wait（秒，可选，≤8）：还没有回执就挂着等；页面断开了就不再等
    let closed = false;
    res.once('close', () => { closed = true; });
    return this.svc.outboxStatus(opId, waitSeconds(wait, OUTBOX_WAIT_MAX) * 1000, () => closed);
  }

  @Post('outbox')
  outbox(@Req() req: Request, @Body() body: OutboxRequest): Promise<OutboxResponse> {
    mustBeOwner(req);
    return this.svc.addOutbox(body);
  }
}
