-- Fly Brain module for Replicanta
-- The organism can evolve, adapt, and interrogate a real larval Drosophila
-- connectome (the rsi-wetware-rs reservoir computer) through this module.
--
-- Public API (registered as the "brain" service for other modules, and
-- documented to the organism in its prompt):
--   brain.run("digits")        -> run the connectome harness (async)
--   brain.optimize("digits")   -> alias for brain.run (older prompts)
--   brain.adapt()              -> L4 drift-rehearsal demo (async)
--   brain.bank()               -> inherited experience bank, as a table
--   brain.last()               -> summary of the last finished run, or ""
--   brain.status()             -> bridge/binary/running summary
--   brain.running()            -> true while a run is in flight
--
-- How the organism calls it: the LLM cannot execute Lua, so it writes the
-- call as text on its own line — brain.optimize("digits") — and the
-- utterance hook below parses and executes it. Runs take minutes on the real
-- connectome, so they are async: the result arrives as a system line and a
-- flybrain_done/flybrain_error event when the subprocess finishes.
--
-- Events: flybrain_done / flybrain_error, declared on the open bus so other
-- modules can subscribe via ctx.events:on("flybrain_done", fn).

function init(ctx)
  local services = ctx.services
  local fly = services.get("flybrain")
  local commands = services.get("commands")
  local hooks = services.get("hooks")
  local ok_ev, events = pcall(function() return ctx.events end)
  -- pcall returns the error object on failure
  if not ok_ev then events = nil end

  ctx.log("fly-brain: module init starting")
  if fly == nil then
    ctx.log("fly-brain: flybrain service not available; is rsi-wetware-rs built?")
    return
  end

  local store = services.get("store")

  local ok_avail, available = pcall(function() return fly:available() end)
  if not ok_avail or not available then
    ctx.log("fly-brain: wetware binary not found; set WETWARE_BIN or build rsi-wetware-rs")
  end

  if events ~= nil then
    events:declare("flybrain_done")
    events:declare("flybrain_error")
  end

  local function normalize(name)
    local p = string.lower(tostring(name or ""))
    p = string.gsub(p, "-", "_")
    p = string.gsub((string.match(p, "^%s*(.-)%s*$")), "%s+", "_")
    return p
  end

  -- Deliver one finished run: remember it in the organism's memory (so the
  -- entity can recall and speak about outcomes, not just log lines), log it,
  -- and emit the bus event. The completion text already says "failed" on
  -- error, so route on that.
  local function deliver(text)
    if store ~= nil then
      pcall(function() store:remember("flybrain", text) end)
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

  -- ------------------------------------------------------------ public API
  local brain = {}

  function brain.available()
    local ok, v = pcall(function() return fly:available() end)
    return ok and v or false
  end

  function brain.running()
    local ok, v = pcall(function() return fly:running() end)
    return ok and v or false
  end

  function brain.status()
    local ok, txt = pcall(function() return fly:status() end)
    if not ok then return "fly brain error: " .. tostring(txt) end
    return txt
  end

  function brain.bank()
    local ok, txt = pcall(function() return fly:bank() end)
    if not ok then return "fly brain error: " .. tostring(txt) end
    return txt
  end

  function brain.last()
    local ok, v = pcall(function() return fly:last() end)
    if not ok or v == nil then return "" end
    local ok_text, txt = pcall(function() return v.text end)
    if not ok_text or txt == nil then return "" end
    return tostring(txt)
  end

  function brain.run(task)
    if brain.running() then
      ctx.log("fly brain: already improving something; wait for the current run")
      return false
    end
    task = tostring(task or "digits")
    local started, err = pcall(function() fly:run_task(task, false, false, deliver) end)
    if not started then
      ctx.log("fly brain: could not start: " .. tostring(err))
      if events ~= nil then events:emit("flybrain_error", tostring(err)) end
      return false
    end
    ctx.log("fly brain: running " .. task .. " harness (async)")
    return true
  end

  -- Alias kept for prompts written against the older RSI-loop wording; the
  -- wetware CLI no longer has autonomy levels or a separate optimize loop.
  function brain.optimize(task)
    return brain.run(task)
  end

  function brain.adapt()
    if brain.running() then
      ctx.log("fly brain: already improving something; wait for the current run")
      return false
    end
    local started, err = pcall(function() fly:adapt(deliver, nil, false, false) end)
    if not started then
      ctx.log("fly brain: could not start adapt: " .. tostring(err))
      if events ~= nil then events:emit("flybrain_error", tostring(err)) end
      return false
    end
    ctx.log("fly brain: drift rehearsal started (async)")
    return true
  end

  -- Other modules (and future entity-code extensions) get the same API.
  services:register("brain", brain)

  -- ------------------------------------------------ entity calling path
  -- Parse one line of organism output for a brain call. Returns true when
  -- something was dispatched. One call per reply, like the hand.
  local function parse_call(line)
    local low = string.lower(line)
    -- brain.optimize("digits") — alias for brain.run("digits")
    local args = string.match(low, "^%s*brain%.optimize%s*%((.-)%)")
    if args ~= nil then
      local task = string.match(args, "[\"'](.-)[\"']")
      brain.run(task or "digits")
      return true
    end
    -- Lua-call sugar without parens: brain.optimize "digits"
    local task2 = string.match(low, "^%s*brain%.optimize%s*[\"'](.-)[\"']")
    if task2 ~= nil then
      brain.run(task2)
      return true
    end
    -- brain.run("digits")
    local run_args = string.match(low, "^%s*brain%.run%s*%((.-)%)")
    if run_args ~= nil then
      local task = string.match(run_args, "[\"'](.-)[\"']")
      brain.run(task or "digits")
      return true
    end
    local run_task2 = string.match(low, "^%s*brain%.run%s*[\"'](.-)[\"']")
    if run_task2 ~= nil then
      brain.run(run_task2)
      return true
    end
    if string.match(low, "^%s*brain%.adapt%s*%(%s*%)") or string.match(low, "^%s*brain%.adapt%s*$") then
      brain.adapt()
      return true
    end
    if string.match(low, "^%s*brain%.bank%s*%(%s*%)") or string.match(low, "^%s*brain%.bank%s*$") then
      ctx.log(brain.bank())
      return true
    end
    return false
  end

  if hooks ~= nil then
    hooks:on("utterance", function(text)
      if text == nil then return end
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
      local verb = normalize(args[1] or "status")
      if verb == "status" or verb == "" then
        return brain.status()
      elseif verb == "bank" then
        return brain.bank()
      elseif verb == "last" then
        local last = brain.last()
        if last == "" then
          return "fly brain: no finished runs yet"
        end
        return "fly brain last run: " .. last
      elseif verb == "run" then
        local task = args[2] or "digits"
        if brain.run(task) then
          return "fly brain: running " .. tostring(task) .. " harness (async)"
        end
        return "fly brain: run not started"
      elseif verb == "optimize" then
        local task = args[2] or "digits"
        if brain.optimize(task) then
          return "fly brain: running " .. tostring(task) .. " harness (async)"
        end
        return "fly brain: run not started"
      elseif verb == "adapt" then
        if brain.adapt() then
          return "fly brain: drift rehearsal started (async)"
        end
        return "fly brain: run not started"
      else
        return "usage: /brain [status|bank|run <task>|optimize <task>|adapt]"
      end
    end)
  end

  ctx.log("fly-brain module loaded; brain.run API armed")
end
