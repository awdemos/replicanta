-- Tendon Hand module for Replicanta
-- The organism drives a real tendon-driven robot hand through this module.
-- Deliberate control paths, both ending at the tendon bridge API:
--   1. The organism writes "hand: <move> [seconds]" on its own line in any
--      utterance; the utterance hook below executes it via the arm service.
--   2. The user types /hand ... in the TUI.
-- The arm service (src/replicanta/tendon_hand.py) is the HTTP client of the
-- tendon bridge server (robot-hand/bridge/server.py, 127.0.0.1:8765).

function init(ctx)
  local services = ctx.services
  local arm = services.get("arm")
  local commands = services.get("commands")
  local hooks = services.get("hooks")

  if arm == nil then
    ctx.log("tendon-hand: arm service not available; is the bridge running on 127.0.0.1:8765?")
    return
  end

  local volition_enabled = true

  -- Execute one "hand: <move> [seconds]" directive against the bridge.
  -- Goals and postures share whitelists in the arm service; try goal
  -- first, fall back to posture (which adds "open").
  local function execute_move(move, dur)
    dur = dur or 4.0
    local ok = pcall(function() return arm:goal(move, dur) end)
    if not ok then
      ok = pcall(function() return arm:posture(move, dur) end)
    end
    if ok then
      ctx.log("hand: '" .. move .. "' sent to the bridge (" .. dur .. "s)")
    else
      ctx.log("hand: unknown move '" .. move .. "'")
    end
    return ok
  end

  -- Deliberate control: scan the organism's own utterances for hand: lines.
  -- (Only role "org" lines fire the utterance event, so user chat text that
  -- happens to contain "hand: ..." never reaches this.)
  if hooks ~= nil then
    hooks:on("utterance", function(text)
      if text == nil then return end
      for line in string.gmatch(tostring(text), "[^\n]+") do
        local move, dur = string.match(line, "^%s*%[?%s*hand%s*:%s*(%a+)%s*([%d%.]*)%s*%]?%s*$")
        if move ~= nil and move ~= "" then
          execute_move(string.lower(move), tonumber(dur))
        end
      end
    end)

    -- Lifecycle reactions. These used to be globals defined inside init()
    -- that nothing ever called; registering them with the hooks service is
    -- what actually wires them to the organism.
    hooks:on("birth", function(_text)
      if volition_enabled then execute_move("reach", 4.0) end
    end)
    hooks:on("cycle", function(text)
      if not volition_enabled then return end
      if text == "wake" then
        execute_move("wave", 3.0)
      elseif text == "sleep" then
        execute_move("release", 5.0)
      end
    end)
    hooks:on("learned", function(_text)
      if volition_enabled then
        pcall(function() arm:emotion({ stress = 0.25, arousal = 0.55 }) end)
      end
    end)
  end

  -- /hand state | posture <name> [dur] | actuator f j side act [dur]
  --       | goal <kind> [dur] | volition [on|off] | emotion s a mood
  if commands ~= nil then
    commands:register("/hand", function(args)
      local verb = (args[1] or "state")
      if verb == "state" or verb == "" then
        local ok, txt = pcall(function() return arm:summary() end)
        if not ok then return "hand error: " .. tostring(txt) end
        return txt
      elseif verb == "posture" then
        local name = args[2] or "open"
        local dur = tonumber(args[3]) or 4.0
        local ok, err = pcall(function() return arm:posture(name, dur) end)
        if not ok then return "hand posture failed: " .. tostring(err) end
        return "hand: posture '" .. name .. "' for " .. dur .. "s"
      elseif verb == "actuator" then
        local f = args[2] or "index"
        local j = args[3] or "mcp"
        local side = args[4] or "flexor"
        local act = tonumber(args[5]) or 0.5
        local dur = tonumber(args[6]) or 4.0
        local ok, err = pcall(function() return arm:actuator(f, j, side, act, dur) end)
        if not ok then return "hand actuator failed: " .. tostring(err) end
        return "hand: " .. f .. "/" .. j .. " " .. side .. " = " .. act .. " for " .. dur .. "s"
      elseif verb == "goal" then
        local kind = args[2] or "reach"
        local dur = tonumber(args[3]) or 4.0
        local ok, err = pcall(function() return arm:goal(kind, dur) end)
        if not ok then return "hand goal failed: " .. tostring(err) end
        return "hand: driving goal '" .. kind .. "' for " .. dur .. "s"
      elseif verb == "volition" then
        local on
        if args[2] then
          on = args[2] == "on" or args[2] == "true"
        else
          on = not volition_enabled  -- bare "/hand volition" toggles
        end
        local ok, err = pcall(function() return arm:volition(on) end)
        if not ok then return "hand volition failed: " .. tostring(err) end
        volition_enabled = on
        return "hand: volition " .. (on and "enabled" or "disabled")
      elseif verb == "emotion" then
        local spec = {}
        if args[2] then spec.stress = tonumber(args[2]) end
        if args[3] then spec.arousal = tonumber(args[3]) end
        if args[4] then spec.mood = args[4] end
        local ok, err = pcall(function() return arm:emotion(spec) end)
        if not ok then return "hand emotion failed: " .. tostring(err) end
        return "hand: emotion updated"
      else
        return "usage: /hand [state|posture <name> [dur]|actuator f j side act [dur]|goal <kind> [dur]|volition [on|off]|emotion s a mood]"
      end
    end)
  end

  ctx.log("tendon-hand module loaded; utterance control armed, volition enabled")
end
