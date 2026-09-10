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

  -- Deliberate control: scan the organism's own utterances for hand: lines.
  -- (Only role "org" lines fire the utterance event, so user chat text that
  -- happens to contain "hand: ..." never reaches this.)

  -- Known moves, multi-word names first so matching prefers them
  -- ("middle_finger" before anything shorter could shadow it).
  local MOVES = {
    "middle_finger", "thumbs_up",
    "reach", "grasp", "release", "point", "wave", "fist",
    "ripple", "pinch", "shaka", "rock", "spock", "open", "ok",
  }
  local MOVES_CSV = table.concat(MOVES, ", ")

  -- Reversal verbs say "go back to neutral" no matter what finger or move
  -- the rest of the phrase names ("retract the middle finger" -> release).
  local REVERSAL = { retract=1, relax=1, rest=1, unclench=1, lower=1, drop=1 }

  -- Aliases resolve after moves: natural phrases the model is likely to
  -- write that are not canonical move names.
  local ALIASES = {
    okay = "ok",
    flip_off = "middle_finger",
    the_finger = "middle_finger",
    the_bird = "middle_finger",
    thumb_up = "thumbs_up",
    thumbsup = "thumbs_up",
    unfold = "open",
    extend = "open",
    close = "fist",
    squeeze = "fist",
    grab = "grasp",
    lift = "reach",
    raise = "reach",
  }

  local function has_word(phrase, w)
    return string.find("_" .. phrase .. "_", "_" .. w .. "_", 1, true) ~= nil
  end

  -- Map the words after "hand:" to a move. The phrase is normalized to
  -- lower_snake; resolution order: reversal verbs anywhere in the phrase,
  -- then a known move anywhere on a word boundary ("extend the middle
  -- finger" -> middle_finger), then aliases, then nothing.
  local function resolve_move(phrase)
    for v in pairs(REVERSAL) do
      if has_word(phrase, v) then return "release" end
    end
    for _, m in ipairs(MOVES) do
      if has_word(phrase, m) then return m end
    end
    for a, m in pairs(ALIASES) do
      if has_word(phrase, a) then return m end
    end
    return nil
  end

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
      -- the organism sees system lines in later context, so the move list
      -- here lets it self-correct on the next turn
      ctx.log("hand: unknown move '" .. move .. "' — moves: " .. MOVES_CSV)
    end
    return ok
  end

  if hooks ~= nil then
    hooks:on("utterance", function(text)
      if text == nil then return end
      for line in string.gmatch(tostring(text), "[^\n]+") do
        -- directive at line start (case-insensitive); trailing words are the
        -- organism's prose; first number on the line, if any, is the duration
        local low = string.lower(line)
        local phrase = string.match(low, "^%s*%[?%s*hand%s*:%s*([^%d]*)")
        if phrase ~= nil then
          phrase = (string.gsub(string.match(phrase, "^%s*(.-)%s*$"), "%s+", "_"))
          if phrase ~= "" then
            local dur = string.match(line, "(%d+%.?%d*)")
            -- fall back to the first token (underscores included) so the
            -- feedback line names what the organism tried, e.g. "thumb_up"
            local move = resolve_move(phrase) or string.match(phrase, "^([%a_]+)")
            execute_move(move, tonumber(dur))
          end
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
