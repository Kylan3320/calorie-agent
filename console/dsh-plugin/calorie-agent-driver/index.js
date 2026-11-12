/**
 * Calorie Agent driver for DeepSeek Harness.
 *
 * Replaces the default LLM agent loop (`@deepseek-ai/dsh-agent-loop`) as the
 * sole agent factory and dispatches per session:
 *
 * - `calorie` (default): claimed user messages go to the local Python backend
 *   (console/server.py POST /api/agent); its reply becomes the assistant
 *   message and its tool executions surface as native tool/call + tool/result
 *   session events. No model call happens on this side — zero relay LLM.
 * - `dsh`: the stock coding agent (ReactLoopAgent, re-exported by the local
 *   agent-loop build) drives the session over the mounted llm/tools/systemPrompt
 *   services.
 *
 * `/dsh` and `/calorie` commands switch the engine for sessions created
 * AFTER the switch; running conversations keep their engine. Mode is tracked
 * in memory; resume falls back to a requestHeader heuristic (a dsh session
 * has logged at least one model request header; a calorie session never does).
 *
 * Lifecycle (turn/step boundaries, event payloads, publication order) is
 * ported from packages/core/agent-loop (ReactLoopAgent + AgentLoop).
 */

import { Service } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import { Inbox, agentEvents, emitAgentEvent } from '@deepseek-ai/dsh-agent'
import { SessionPreparation } from '@deepseek-ai/dsh-session'
import { createAssistantMessage, createToolResultMessage } from '@deepseek-ai/dsh-llm'
import { createScope } from '@deepseek-ai/dsh-scope'
import { ReactLoopAgent, turnBoundaryProjectionDefinition } from '@deepseek-ai/dsh-agent-loop'

const DISPOSED = { kind: 'disposed' }

function messageText(message) {
  const content = message?.content
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content.filter((block) => block?.type === 'text').map((block) => block.text ?? '').join('\n')
  }
  return ''
}

/** One session-bound driver: claims the inbox, calls the Python backend, appends events. */
class CalorieAgent {
  constructor(loopCtx, id, options, session, driverConfig) {
    this.loopCtx = loopCtx
    this.id = id
    this.options = options ?? {}
    this.session = session
    this.driverConfig = driverConfig
    this.dispatch = agentEvents(loopCtx, this)
    this.inbox = new Inbox(session, {
      inserted: (message) => { this.dispatch.emit('agent/inbox/inserted', { message }) },
      discarded: (message) => { this.dispatch.emit('agent/inbox/discarded', { message }) },
      claimed: (message, turn) => { this.dispatch.emit('agent/inbox/claimed', { message, turn }) },
    })
    const lastTurn = loopCtx.sessionProjections?.stateOf?.(session, 'turnBoundary')?.lastTurn ?? 0
    this.phase = { kind: 'idle', lastTurn }
    this.activityDone = Promise.resolve()
    this.scope = createScope(loopCtx, this)
    this.ctx = this.scope.ctx.extend({ agent: this })
  }

  get status() {
    return this.phase.kind === 'idle' || this.phase.kind === 'maintenance' ? 'idle' : 'running'
  }

  setPhase(next) {
    const previous = this.status
    this.phase = next
    if (this.status !== previous) this.dispatch.emit('agent/status', { status: this.status })
  }

  send(message, target, wakeup) {
    const wakingAfterAbort = wakeup && this.phase.kind !== 'idle' && this.phase.abort.signal.aborted
    const resolvedTarget = wakingAfterAbort ? 'next-turn' : target
    this.inbox.splice(resolvedTarget, Infinity, 0, [message])
    if (wakeup) this.wakeDriver(wakingAfterAbort)
  }

  followup(message) { this.send(message, 'next-turn', true) }

  steer(message) { this.send(message, 'next-step', true) }

  inject(message) { this.send(message, 'next-step', false) }

  cancel(cause, options = {}) {
    if (!options.keepInbox) {
      this.inbox.clear()
      if (this.phase.kind !== 'idle') this.phase.wakeRequested = false
    }
    if (this.phase.kind !== 'idle') this.phase.abort.abort(cause)
  }

  runMaintenance(job) {
    if (this.phase.kind !== 'idle') throw new Error(`agent "${this.id}" already has active work`)
    let release
    const done = new Promise((resolve) => { release = resolve })
    const maintenance = { kind: 'maintenance', abort: new AbortController(), lastTurn: this.phase.lastTurn, wakeRequested: false }
    this.setPhase(maintenance)
    this.activityDone = done
    return (async () => {
      try {
        return await job(maintenance.abort.signal)
      } finally {
        this.setPhase({ kind: 'idle', lastTurn: maintenance.lastTurn })
        if (maintenance.wakeRequested && this.inbox.hasPending) this.wakeDriver()
        release()
      }
    })()
  }

  wakeDriver(wakeAfterAbort = false) {
    if (this.phase.kind !== 'idle') {
      const reason = this.phase.abort?.signal?.reason
      if (reason?.kind !== 'disposed' && (this.phase.kind === 'maintenance' || wakeAfterAbort)) {
        this.phase.wakeRequested = true
      }
      return
    }
    let release
    const driver = new Promise((resolve) => { release = resolve })
    this.activityDone = driver
    this.setPhase({ kind: 'running', abort: new AbortController(), turn: this.phase.lastTurn, step: 0, wakeRequested: false })
    this.loopCtx.agents.withInitiator(this, () => this.kick()).then(release, release)
  }

  async whenIdle() {
    let activity
    do {
      await (activity = this.activityDone)
    } while (activity !== this.activityDone)
  }

  throwError(error) {
    const turn = this.phase.kind === 'running' ? this.phase.turn : this.phase.lastTurn
    const step = this.phase.kind === 'running' ? this.phase.step : 0
    this.dispatch.emit('agent/error', { turn, step, error })
    throw error
  }

  async kick() {
    try {
      while (await this.turn()) {}
    } catch {
      // Reported failures and cancellation are contained at the driver boundary.
    } finally {
      if (this.phase.kind === 'running') {
        const { turn, wakeRequested } = this.phase
        this.setPhase({ kind: 'idle', lastTurn: turn })
        if (wakeRequested && this.inbox.hasPending) this.wakeDriver()
      }
    }
  }

  async turn() {
    if (this.phase.kind !== 'running') {
      this.throwError(new Error(`agent "${this.id}": turn without driver reservation`))
    }
    const phase = this.phase
    const signal = phase.abort.signal
    signal.throwIfAborted()
    const turn = phase.turn + 1
    this.session.append('turn/start', { turn })
    phase.turn = turn
    let turnEnds = null
    let target = 'next-turn'
    try {
      while (true) {
        signal.throwIfAborted()
        const step = phase.step + 1
        const claimed = this.inbox.claim(target, turn)
        if (claimed.length === 0) {
          turnEnds = { kind: 'completed' }
          return false
        }
        this.session.append('step/start', { turn, step })
        phase.step = step
        try {
          for (const message of claimed) {
            this.session.append('user/message', message, { surfaceOp: 'append' })
          }
          turnEnds = await this.runStep(turn, step, claimed, signal)
        } finally {
          this.session.append('step/end', { turn, step })
        }
        signal.throwIfAborted()
        if (this.inbox.nextStep.length === 0) break
        target = 'next-step'
      }
    } catch (error) {
      if (signal.aborted) {
        turnEnds = { kind: 'aborted', reason: signal.reason }
        throw error
      }
      turnEnds = { kind: 'error', error: { message: String(error?.message ?? error), code: 'UNKNOWN' } }
      this.throwError(error)
    } finally {
      this.session.append('turn/end', {
        turn,
        reason: turnEnds ?? { kind: 'error', error: { message: 'turn exited without an ending', code: 'UNKNOWN' } },
      })
    }
    if (!this.inbox.hasPending) return false
    phase.abort = new AbortController()
    phase.wakeRequested = false
    phase.step = 0
    return true
  }

  async runStep(turn, step, claimed, signal) {
    const text = claimed.map((message) => messageText(message)).filter(Boolean).join('\n')
    const source = {
      provider: this.options.provider ?? 'calorie-local',
      model: this.options.model ?? 'calorie-agent',
    }
    let result
    try {
      result = await this.callBackend(text, signal, `${turn}:${step}`)
    } catch (error) {
      if (signal.aborted) throw error
      // Backend unreachable: show it in the chat instead of an opaque error turn.
      const text_block = [{ type: 'text', text: `桥接后端不可达（${this.driverConfig.endpoint}）：${String(error?.message ?? error)}` }]
      this.session.append(
        'assistant/message',
        { turn, step, message: createAssistantMessage({ content: text_block, source }) },
        { surfaceOp: 'append', sourceEventSeqs: [] },
      )
      return { kind: 'completed' }
    }

    const events = result?.audit_status === 'unavailable' ? [] : (Array.isArray(result?.tool_events) ? result.tool_events : [])
    if (events.length > 0) {
      // Agent-loop contract: the tool-call block lives inside the assistant
      // message content AND as a durable tool/call event; the result cites the
      // call seq so replay stays balanced.
      const blocks = events.map((event) => ({
        type: 'tool-call',
        id: String(event.callId),
        name: String(event.name),
        arguments: typeof event.arguments === 'string' ? event.arguments : JSON.stringify(event.arguments ?? {}),
      }))
      this.session.append(
        'assistant/message',
        { turn, step, message: createAssistantMessage({ content: blocks, source }) },
        { surfaceOp: 'append', sourceEventSeqs: [] },
      )
      for (const event of events) {
        const callId = String(event.callId)
        const args = typeof event.arguments === 'string' ? event.arguments : JSON.stringify(event.arguments ?? {})
        const callSeq = this.session.append('tool/call', { turn, step, callId, name: String(event.name), arguments: args }).seq
        this.session.append(
          'tool/result',
          {
            turn,
            step,
            message: createToolResultMessage({
              callId,
              content: [{ type: 'text', text: String(event.content ?? '') }],
              isError: Boolean(event.isError),
            }),
          },
          { surfaceOp: 'append', sourceEventSeqs: [callSeq] },
        )
      }
    }

    const reply = String(result?.reply ?? '')
    this.session.append(
      'assistant/message',
      { turn, step, message: createAssistantMessage({ content: [{ type: 'text', text: reply }], source }) },
      { surfaceOp: 'append', sourceEventSeqs: [] },
    )
    if (result?.delivery_reference) {
      const endpoint = this.driverConfig.endpoint.replace(/\/$/, '') + '/presented'
      const reported = await fetch(endpoint, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ delivery_reference: result.delivery_reference }), signal,
      })
      if (!reported.ok) throw new Error(`presentation report HTTP ${reported.status}`)
    }
    return { kind: 'completed' }
  }

  async callBackend(text, signal, messageId) {
    const timeout = AbortSignal.timeout(this.driverConfig.timeoutMs ?? 180000)
    const fused = signal ? AbortSignal.any([signal, timeout]) : timeout
    const response = await fetch(this.driverConfig.endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        session_id: this.id,
        message_id: messageId,
        preset: this.driverConfig.preset || 'live',
        ...(this.driverConfig.planner ? { planner: this.driverConfig.planner } : {}),
        ...(this.driverConfig.storeKind ? { store_kind: this.driverConfig.storeKind } : {}),
      }),
      signal: fused,
    })
    if (!response.ok) throw new Error(`backend HTTP ${response.status}`)
    return await response.json()
  }
}

/**
 * Agent factory replacing the default loop. Sessions are created per client
 * request by the web app; every created/resumed session binds to this driver
 * because it is the only registered factory.
 */
export class CalorieAgentDriver extends Service {
  static inject = ['agents', 'sessions', 'sessionProjections', 'commands', 'llm', 'tools', 'systemPrompt']

  static Config = z.object({
    endpoint: z.string().default('http://127.0.0.1:8765/api/agent'),
    preset: z.string().default('live'),
    planner: z.string().default(''),
    storeKind: z.string().default(''),
    timeoutMs: z.number().default(180000),
  })

  constructor(ctx, config) {
    super(ctx, 'calorieAgentDriver')
    this.config = config
    /** Engine for sessions created from now on; /dsh and /calorie flip it. */
    this.mode = 'calorie'
    /** sessionId -> engine, live for this process; resume falls back to a heuristic. */
    this.modes = new Map()
    // Downstream plugins (agent-instructions, trajectory) hard-require this projection.
    ctx.sessionProjections.register(turnBoundaryProjectionDefinition)
    // The disabled agent-loop row used to register these prompt variables; the
    // dsh engine's persona section references {{cwd}} (and provider/model).
    ctx.systemPrompt.variable('provider', context => context.agent?.options.provider)
    ctx.systemPrompt.variable('model', context => context.agent?.options.model)
    ctx.systemPrompt.variable('cwd', context => context.agent?.session.header.cwd)
    ctx.effect(() => ctx.agents.setFactory(this), 'calorieAgentDriver.setFactory()')
    ctx.commands.register({
      name: 'dsh',
      description: '切换引擎：之后新建的会话使用 DSH 编码助手（当前对话不变）',
      recordInput: false,
      handler: () => {
        console.log('[calorie-driver] /dsh command invoked')
        this.mode = 'dsh'
        return { kind: 'success', text: '已切换：之后新建的会话使用 DSH 编码助手引擎；当前对话保持不变。' }
      },
    })
    ctx.commands.register({
      name: 'calorie',
      description: '切换引擎：之后新建的会话使用热量助手（当前对话不变）',
      recordInput: false,
      handler: () => {
        console.log('[calorie-driver] /calorie command invoked')
        this.mode = 'calorie'
        return { kind: 'success', text: '已切换：之后新建的会话使用热量助手引擎；当前对话保持不变。' }
      },
    })
  }

  async createAgent(ownerCtx, options) {
    const mode = this.mode
    console.log(`[calorie-driver] createAgent ${options.sessionId} mode=${mode}`)
    this.modes.set(options.sessionId, mode)
    const preparation = SessionPreparation.create(this.ctx.sessions.prepare(options.sessionId, {
      ...(options.seed === undefined ? {} : { seed: options.seed }),
      ...(options.meta === undefined ? {} : { meta: options.meta }),
      ...(options.inheritedEventCount === undefined ? {} : { inheritedEventCount: options.inheritedEventCount }),
    }))
    return await this.setupAndPublish(ownerCtx, options.sessionId, preparation, options.agentOptions ?? {}, options.setup, 'startup', mode)
  }

  async resume(ownerCtx, options) {
    const persistence = this.ctx.get('sessionPersistence')
    if (persistence === undefined) {
      throw new Error('cannot resume: session persistence is not configured (load a dsh-session-persistence backend)')
    }
    const preparation = await persistence.prepare(options.resumeSessionId)
    const remembered = this.modes.get(options.resumeSessionId)
    // Heuristic: a dsh session has logged a model request header; a calorie
    // session never does (the bridge appends no request/header events).
    const mode = remembered ?? (preparation.session.requestHeader() !== undefined ? 'dsh' : 'calorie')
    this.modes.set(options.resumeSessionId, mode)
    return await this.setupAndPublish(ownerCtx, options.resumeSessionId, preparation, options.agentOptions ?? {}, options.setup, 'resume', mode)
  }

  async setupAndPublish(ownerCtx, id, preparation, agentOptions, setup, source, mode) {
    try {
      const session = preparation.session
      const agent = mode === 'dsh'
        ? new ReactLoopAgent(this.ctx, id, agentOptions, session)
        : new CalorieAgent(this.ctx, id, agentOptions, session, this.config)
      const commit = await setup?.(agent.ctx)
      commit?.commit?.()
      const detachSession = agent.ctx.sessions.enter(session)
      const detachAgent = this.ctx.agents.enter(agent, ownerCtx.agent)
      agent.ctx.sessions.announce(session)
      this.ctx.agents.announce(agent)
      emitAgentEvent(this.ctx, agent, 'agent/session-start', { source })
      let disposed = false
      const dispose = async () => {
        if (disposed) return
        disposed = true
        try {
          agent.cancel(DISPOSED)
          await agent.whenIdle()
          await agent.scope.dispose()
        } finally {
          detachAgent()
          detachSession()
        }
      }
      return { agent, dispose }
    } finally {
      preparation[Symbol.dispose]?.()
    }
  }
}

export default CalorieAgentDriver
