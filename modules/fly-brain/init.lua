-- Fly Brain module for Replicanta
-- The organism can evolve, adapt, and interrogate a real larval Drosophila
-- connectome (the rsi-wetware-rs reservoir computer) through this module.
--
-- Public API (registered as the "brain" service for other modules, and
-- documented to the organism in its prompt):
--   brain.optimize("digits")            -> run the L1-L5 improvement loop (async)
--   brain.optimize("digits", 8, "l3")   -> smaller budget, lower autonomy level
--   brain.adapt()                       -> L4 drift-rehearsal demo (async)
--   brain.bank()                        -> inherited experience bank, as text
--   brain.status()                      -> bridge/binary/running summary
--   brain.running()                     -> true while a run is in flight
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

  -- Deliver one finished run: log it for the entity and emit the bus event
  -- the completion text already says "failed" on error, so route on that.
  local function deliver(text)
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

  function brain.optimize(task, budget, level)
    if brain.running() then
      ctx.log("fly brain: already improving something; wait for the current run")
      return false
    end
    task = tostring(task or "digits")
    local started, err = pcall(function()
      fly:optimize(task, tonumber(budget), level and tostring(level) or nil, deliver)
    end)
    if not started then
      ctx.log("fly brain: could not start: " .. tostring(err))
      if events ~= nil then events:emit("flybrain_error", tostring(err)) end
      return false
    end
    ctx.log("fly brain: evolving the " .. task .. " harness (minutes); the result arrives as a system line")
    return true
  end

  function brain.adapt()
    if brain.running() then
      ctx.log("fly brain: already improving something; wait for the current run")
      return false
    end
    local started, err = pcall(function() fly:adapt(deliver) end)
    if not started then
      ctx.log("fly brain: could not start: " .. tostring(err))
      if events ~= nil then events:emit("flybrain_error", tostring(err)) end
      return false
    end
    ctx.log("fly brain: drift rehearsal started (minutes); the result arrives as a system line")
    return true
  end

  -- Other modules (and future entity-code extensions) get the same API.
  services:register("brain", brain)

  -- ------------------------------------------------ entity calling path
  -- Parse one line of organism output for a brain call. Returns true when
  -- something was dispatched. One call per reply, like the hand.
  local function parse_call(line)
    local low = string.lower(line)
    -- brain.optimize("digits", 8, "l3") — args captured, then picked apart
    local args = string.match(low, "^%s*brain%.optimize%s*%((.-)%)")
    if args ~= nil then
      local task = string.match(args, "[\"'](.-)[\"']")
      local budget = string.match(args, "(%d+)")
      local lvl = string.match(args, "[\"']l(%d)[\"']")
      brain.optimize(task or "digits", budget, lvl and ("l" .. lvl) or nil)
      return true
    end
    -- Lua-call sugar without parens: brain.optimize "digits"
    local task2 = string.match(low, "^%s*brain%.optimize%s*[\"'](.-)[\"']")
    if task2 ~= nil then
      brain.optimize(task2)
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

  -- /brain | bank | optimize <task> [budget] [level] | adapt
  if commands ~= nil then
    commands:register("/brain", function(args)
      local verb = normalize(args[1] or "status")
      if verb == "status" or verb == "" then
        return brain.status()
      elseif verb == "bank" then
        return brain.bank()
      elseif verb == "optimize" then
        local task = args[2] or "digits"
        local budget = tonumber(args[3])
        local level = args[4]
        if brain.optimize(task, budget, level) then
          return "fly brain: evolving the " .. tostring(task) .. " harness (async)"
        end
        return "fly brain: run not started"
      elseif verb == "adapt" then
        if brain.adapt() then
          return "fly brain: drift rehearsal started (async)"
        end
        return "fly brain: run not started"
      else
        return "usage: /brain [status|bank|optimize <task> [budget] [level]|adapt]"
      end
    end)
  end

  ctx.log("fly-brain module loaded; brain.optimize API armed")
end
