-- Fly-brain module for Replicanta — pure Lua over the capability bridges.
--
-- The organism can evolve, adapt, and interrogate a real larval Drosophila
-- connectome (the rsi-wetware-rs reservoir computer) through this module.
-- The module owns all behavior: it spawns the wetware CLI via
-- ctx.process (plain pipes, stderr merged), parses reports with ctx.json,
-- summarizes them, and remembers outcomes in the organism's store.
-- Python only discovers the binary (externals service).
--
-- Public API (registered as the "brain" service for other modules, and
-- documented to the organism in its prompt):
--   brain.run("digits")        -> run the connectome harness (async)
--   brain.run("digits", true)  -> same, blocking; returns the report table
--   brain.optimize("digits")   -> alias for brain.run (older prompts)
--   brain.adapt()              -> L4 drift-rehearsal demo (async)
--   brain.bank()               -> experience bank, as text
--   brain.last()               -> last finished run {ok, text, report}, or nil
--   brain.status()             -> binary/running/last-run summary
--   brain.running()            -> true while a run is in flight
--   brain.available()          -> true when the wetware CLI resolves
--
-- How the organism calls it: the LLM cannot execute Lua, so it writes the
-- call as text on its own line — brain.optimize("digits") — and the
-- utterance hook below parses and executes it. Runs take minutes on the
-- real connectome, so they are async: the result lands in the organism's
-- memory (kind flybrain) and a flybrain_done/flybrain_error event fires
-- when the subprocess finishes.
--
-- Events: flybrain_done / flybrain_error, declared on the open bus so other
-- modules can subscribe via ctx.events:on("flybrain_done", fn).

function init(ctx)
  local services = ctx.services
  local externals = services.get("externals")
  local store = services.get("store")
  local commands = services.get("commands")
  local hooks = services.get("hooks")
  local proc = ctx.process
  local json = ctx.json
  local ok_ev, events = pcall(function() return ctx.events end)
  -- pcall returns the error object on failure
  if not ok_ev then events = nil end
  local ok_call, svc_call = pcall(function() return ctx.call end)
  if not ok_call then svc_call = nil end

  ctx.log("fly-brain: module init starting")

  if events ~= nil then
    events:declare("flybrain_done")
    events:declare("flybrain_error")
  end

  local TASKS = { digits = true, timeseries = true }
  local DEFAULT_TIMEOUT = 1800.0
  local QUICK_TIMEOUT = 120.0

  local state = {
    running = false,
    kind = nil,
    last = nil, -- {ok=bool, text=string, report=table|nil}
  }

  local function binary()
    if externals == nil then
      return nil
    end
    local ok, path = pcall(function()
      return externals:wetware_binary()
    end)
    if ok then
      return path
    end
    return nil
  end

  local function available()
    return binary() ~= nil
  end

  -- Deliver one finished run: remember it in the organism's memory (so the
  -- entity can recall and speak about outcomes, not just log lines), log
  -- it, and emit the bus event. The completion text already says "failed"
  -- on error, so route on that.
  local function deliver(text)
    if store ~= nil then
      pcall(function()
        store:remember("flybrain", text)
      end)
    end
    ctx.log(text)
    if events ~= nil then
      if string.match(text, "failed") then
        events:emit("flybrain_error", text)
      else
        events:emit("flybrain_done", text)
      end
    end
  end

  -- ------------------------------------------------------------ summaries
  local function is_number(v)
    return type(v) == "number"
  end

  local function summarize_run(report)
    local parts = { "fly brain: " .. tostring(report.task or "?") .. " done" }
    local test = report.test
    local default = report.default_test
    local improvement = report.improvement
    if is_number(test) then
      local score = string.format("test %.4f", test)
      if is_number(default) then
        score = score .. string.format(" (default %.4f)", default)
      end
      if is_number(improvement) then
        score = score .. string.format(", improvement %+.4f", improvement)
      end
      parts[#parts + 1] = score
    end
    if type(report.bank_entries) == "number" then
      parts[#parts + 1] = string.format("bank %d entries", report.bank_entries)
    end
    return table.concat(parts, "; ")
  end

  local function summarize_adapt(report)
    local parts = { "fly brain: l4 drift rehearsal done" }
    local gated = report.l4_gated_val
    local naive = report.l4_naive_val
    local advantage = report.gate_advantage
    if is_number(gated) and is_number(naive) then
      parts[#parts + 1] = string.format("gated %.4f vs naive %.4f", gated, naive)
      if is_number(advantage) then
        parts[#parts + 1] = string.format("(%+.4f)", advantage)
      end
    end
    for _, key in ipairs({ "adaptations", "rollbacks" }) do
      local value = report[key]
      if type(value) == "number" then
        parts[#parts + 1] = string.format("%d %s", value, key)
      end
    end
    return table.concat(parts, " ")
  end

  local function summarize(kind, report)
    if kind == "run" then
      return summarize_run(report)
    end
    if kind == "adapt" then
      return summarize_adapt(report)
    end
    return "fly brain: " .. tostring(kind) .. " finished"
  end

  -- ------------------------------------------------------------ cli helpers
  local function require_binary()
    local bin = binary()
    if bin == nil then
      error("wetware binary not found; build rsi-wetware-rs or set WETWARE_BIN")
    end
    return bin
  end

  local function parse_output(text)
    local ok, report = pcall(function()
      return json.parse(text)
    end)
    if not ok then
      error("wetware returned unparsable output: " .. text:sub(1, 200))
    end
    return report
  end

  local function tail(text)
    return text:gsub("%s+", " "):sub(-300)
  end

  -- Run the CLI to completion on the calling thread; returns the parsed
  -- report table (raises a Lua-catchable error on failure).
  local function run_sync(args, timeout)
    local bin = require_binary()
    local argv = { bin }
    for _, a in ipairs(args) do
      argv[#argv + 1] = a
    end
    local pid = proc.spawn(argv, {})
    local ok_wait, code = pcall(function()
      return proc.wait(pid, timeout)
    end)
    if not ok_wait then
      pcall(function()
        proc.kill(pid)
      end)
      error("wetware timed out: " .. tostring(code))
    end
    local out = proc.output(pid)
    pcall(function()
      proc.kill(pid)
    end) -- reap
    if code ~= 0 then
      error(string.format("wetware exited %d: %s", code, tail(out)))
    end
    return parse_output(out)
  end

  -- Spawn the CLI and deliver the summary asynchronously via on_exit.
  local function run_async(kind, args)
    if state.running then
      error("fly brain is already running a " .. tostring(state.kind))
    end
    local bin = require_binary()
    state.running = true
    state.kind = kind
    local argv = { bin }
    for _, a in ipairs(args) do
      argv[#argv + 1] = a
    end
    local pid -- pre-declared: the on_exit closure below must capture this
    -- local, not the outer scope (a local is out of scope inside its own
    -- initializer expression)
    pid = proc.spawn(argv, {
      on_exit = function(code)
        state.running = false
        local ok = true
        local summary = ""
        local report = nil
        local out = ""
        pcall(function()
          out = proc.output(pid)
        end)
        if code == 0 then
          local okp, rep = pcall(parse_output, out)
          if okp then
            report = rep
            summary = summarize(kind, report)
          else
            ok = false
            summary = "fly brain: " .. kind .. " failed: " .. tostring(rep)
          end
        else
          ok = false
          summary = string.format(
            "fly brain: %s failed: wetware exited %d: %s",
            kind,
            code,
            tail(out)
          )
        end
        state.last = { ok = ok, text = summary, report = report }
        deliver(summary)
        -- Reap the finished child so it stops counting against the
        -- 2-process cap.
        pcall(function()
          proc.kill(pid)
        end)
      end,
    })
    return true
  end

  -- ------------------------------------------------------------ public API
  local brain = {}

  function brain.available()
    return available()
  end

  function brain.running()
    return state.running
  end

  function brain.status()
    local lines = {}
    local bin = binary()
    lines[#lines + 1] = "fly brain bridge: "
      .. (bin and "ready" or "no binary (set WETWARE_BIN)")
    if bin then
      lines[#lines + 1] = "binary: " .. bin
    end
    lines[#lines + 1] = "running: " .. (state.running and "yes" or "no")
    if state.last ~= nil then
      lines[#lines + 1] = "last run: " .. state.last.text
    end
    return table.concat(lines, "\n")
  end

  function brain.last()
    return state.last
  end

  function brain.bank()
    local report = run_sync({ "bank" }, QUICK_TIMEOUT)
    local lines = {}
    for _, key in ipairs({ "policy", "entries" }) do
      if report[key] ~= nil then
        lines[#lines + 1] = tostring(key) .. ": " .. tostring(report[key])
      end
    end
    if #lines == 0 then
      return "fly brain: experience bank is empty"
    end
    return table.concat(lines, "\n")
  end

  function brain.run(task, wait, sample)
    task = string.lower(tostring(task or "digits"))
    if not TASKS[task] then
      error("unknown task '" .. task .. "'; try digits, timeseries")
    end
    local args = {}
    if sample then
      args[#args + 1] = "--sample"
    end
    args[#args + 1] = "run"
    args[#args + 1] = task
    if wait then
      local report = run_sync(args, DEFAULT_TIMEOUT)
      local summary = summarize_run(report)
      state.last = { ok = true, text = summary, report = report }
      deliver(summary)
      return report
    end
    return run_async("run", args)
  end

  -- Alias kept for prompts written against the older RSI-loop wording; the
  -- wetware CLI no longer has autonomy levels or a separate optimize loop.
  function brain.optimize(task, wait, sample)
    return brain.run(task, wait, sample)
  end

  function brain.adapt(wait, sample, seed)
    local args = {}
    if sample then
      args[#args + 1] = "--sample"
    end
    args[#args + 1] = "adapt"
    if seed ~= nil then
      args[#args + 1] = "--seed"
      args[#args + 1] = tostring(seed)
    end
    if wait then
      local report = run_sync(args, DEFAULT_TIMEOUT)
      local summary = summarize_adapt(report)
      state.last = { ok = true, text = summary, report = report }
      deliver(summary)
      return report
    end
    return run_async("adapt", args)
  end

  -- Other modules (and future entity-code extensions) get the same API.
  services:register("brain", brain)

  -- ------------------------------------------------ entity calling path
  -- Entity actuation gate: runs/adapt/bank spawn processes (or block for
  -- minutes), and the utterance hook is entity-initiated — consult the
  -- organism's entity_actuation flag before executing. User paths (/brain)
  -- are not gated. Hosts without the facade default to enabled.
  local function actuation_enabled()
    local org = services.get("organism")
    if org == nil then
      return true
    end
    local ok, flag = pcall(function()
      return org:entity_actuation()
    end)
    if not ok or flag == nil then
      return true
    end
    return flag and true or false
  end

  -- Parse one line of organism output for a brain call. Returns true when
  -- something was dispatched. One call per reply, like the hand.
  -- ctx.call (when the host provides it) is the shared tolerant parser:
  -- it handles brain.run("digits") / brain.optimize("digits") and their
  -- no-paren sugar, routing them to the registered brain service; the bare
  -- brain.adapt / brain.bank forms stay local patterns (not a svc.method
  -- call shape).
  local function parse_call(line)
    local low = string.lower(line)
    local is_bank = false
    local bare = string.match(low, "^%s*brain%.adapt%s*%(%s*%)%s*$") ~= nil
      or string.match(low, "^%s*brain%.adapt%s*$") ~= nil
    if not bare then
      is_bank = string.match(low, "^%s*brain%.bank%s*%(%s*%)%s*$") ~= nil
        or string.match(low, "^%s*brain%.bank%s*$") ~= nil
      bare = is_bank
    end
    if bare then
      if not actuation_enabled() then
        ctx.log("actuation disabled — skipping " .. (is_bank and "brain.bank" or "brain.adapt"))
        return true
      end
      if is_bank then
        local ok, text = pcall(brain.bank)
        ctx.log(ok and text or tostring(text))
      else
        local ok, err = pcall(brain.adapt)
        if not ok then
          ctx.log("fly brain: " .. tostring(err))
        end
      end
      return true
    end
    if svc_call ~= nil then
      local parts = {svc_call.parse(low)}
      local method = parts[2]
      if parts[1] == "brain" and method ~= nil then
        if method ~= "run" and method ~= "optimize" and method ~= "adapt" and method ~= "bank" then
          return false
        end
        if not actuation_enabled() then
          ctx.log("actuation disabled — skipping brain." .. method)
          return true
        end
        -- Reconstruct without a wait flag: the utterance path must stay
        -- async (a blocking run would stall the event-dispatch thread);
        -- the legacy parser dropped it by construction.
        local res, err
        if method == "adapt" then
          res, err = svc_call("brain.adapt()")
        elseif method == "bank" then
          res, err = svc_call("brain.bank()")
        else
          res, err = svc_call('brain.run("' .. tostring(parts[3] or "digits") .. '")')
        end
        if err ~= nil then
          ctx.log("fly brain: " .. tostring(err))
        elseif method == "bank" and res ~= nil then
          ctx.log(res)
        end
        return true
      end
      return false
    end
    -- Legacy fallback for ctx shapes without ctx.call (older hosts): the
    -- quoted paren and sugar forms only.
    local task = string.match(low, "^%s*brain%.optimize%s*%(%s*[\"'](.-)[\"']%s*%)")
      or string.match(low, "^%s*brain%.optimize%s*[\"'](.-)[\"']")
      or string.match(low, "^%s*brain%.run%s*%(%s*[\"'](.-)[\"']%s*%)")
      or string.match(low, "^%s*brain%.run%s*[\"'](.-)[\"']")
    if task ~= nil then
      if not actuation_enabled() then
        ctx.log("actuation disabled — skipping brain.run")
        return true
      end
      local ok, err = pcall(function()
        brain.run(task)
      end)
      if not ok then
        ctx.log("fly brain: " .. tostring(err))
      end
      return true
    end
    return false
  end

  if hooks ~= nil then
    hooks:on("utterance", function(text)
      if text == nil then
        return
      end
      for ln in string.gmatch(tostring(text), "[^\n]+") do
        if parse_call(ln) then
          ctx.log("fly-brain: dispatched from line: " .. ln)
          break
        end
      end
    end)
  end

  -- /brain | bank | last | run <task> | optimize <task> | adapt
  if commands ~= nil then
    commands:register("/brain", function(args)
      local function normalize(name)
        local p = string.lower(tostring(name or ""))
        p = string.gsub(p, "-", "_")
        p = string.gsub((string.match(p, "^%s*(.-)%s*$")), "%s+", "_")
        return p
      end
      local verb = normalize(args[1] or "status")
      if verb == "status" or verb == "" then
        return brain.status()
      elseif verb == "bank" then
        local ok, text = pcall(brain.bank)
        if ok then
          return text
        end
        return "fly brain error: " .. tostring(text)
      elseif verb == "last" then
        if state.last == nil then
          return "fly brain: no finished runs yet"
        end
        return "fly brain last run: " .. state.last.text
      elseif verb == "run" or verb == "optimize" then
        local task = args[2] or "digits"
        local ok, err = pcall(function()
          brain.run(task)
        end)
        if ok then
          return "fly brain: running " .. tostring(task) .. " harness (async)"
        end
        return "fly brain: run not started (" .. tostring(err) .. ")"
      elseif verb == "adapt" then
        local ok, err = pcall(brain.adapt)
        if ok then
          return "fly brain: drift rehearsal started (async)"
        end
        return "fly brain: run not started (" .. tostring(err) .. ")"
      else
        return "usage: /brain [status|bank|last|run <task>|optimize <task>|adapt]"
      end
    end)
  end

  if not available() then
    ctx.log("fly-brain: wetware binary not found; set WETWARE_BIN or build rsi-wetware-rs")
  end

  ctx.log("fly-brain module loaded; brain.run API armed")
end
