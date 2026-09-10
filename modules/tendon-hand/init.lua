-- Tendon Hand module for Replicanta
-- The organism drives a real tendon-driven robot hand through this module.
--
-- Public API (registered as the "hand" service for other modules, and
-- documented to the organism in its prompt):
--   hand.move("wave")            -> dispatch a goal/posture by name
--   hand.move("fist", 3)         -> with a duration in seconds
--   hand.move("extend the middle finger")  -> phrases resolve to moves
--   hand.posture("open", 2)      -> explicit posture
--   hand.state()                 -> human-readable bridge summary
--   hand.moves()                 -> comma-separated valid move names
--   hand.volition(true|false)    -- toggle self-directed movement
--
-- How the organism calls it: the LLM cannot execute Lua, so it writes the
-- call as text on its own line — hand.move("wave", 3) — and the utterance
-- hook below parses and executes it. The legacy "hand: wave 3" directive
-- form still works. Both paths funnel into hand.move, which talks to the
-- arm service (src/replicanta/tendon_hand.py), the HTTP client of the
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

  -- ------------------------------------------------------------ vocabulary
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

  -- Map a phrase to a move. The phrase is normalized to lower_snake;
  -- resolution order: reversal verbs anywhere in the phrase, then a known
  -- move anywhere on a word boundary ("extend the middle finger" ->
  -- middle_finger), then aliases, then nothing.
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

  local function normalize(name)
    local p = string.lower(tostring(name or ""))
    p = string.gsub(p, "-", "_")
    p = string.gsub((string.match(p, "^%s*(.-)%s*$")), "%s+", "_")
    return p
  end

  -- ------------------------------------------------------------ public API
  local hand = {}

  -- Dispatch a move by name or natural phrase. Goals and postures share
  -- whitelists in the arm service; try goal first, fall back to posture
  -- (which adds "open"). Returns true when the bridge accepted the move.
  function hand.move(name, dur)
    if name == nil then return false end
    dur = tonumber(dur) or 4.0
    local phrase = normalize(name)
    if phrase == "" then return false end
    -- fall back to the first token so the feedback line names what the
    -- organism tried when it invents an unknown move, e.g. "cartwheel"
    local move = resolve_move(phrase) or string.match(phrase, "^([%a_]+)")
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

  function hand.posture(name, dur)
    dur = tonumber(dur) or 4.0
    local ok, err = pcall(function() return arm:posture(normalize(name), dur) end)
    if not ok then
      ctx.log("hand: posture failed: " .. tostring(err))
      return false
    end
    ctx.log("hand: posture '" .. normalize(name) .. "' for " .. dur .. "s")
    return true
  end

  function hand.state()
    local ok, txt = pcall(function() return arm:summary() end)
    if not ok then return "hand error: " .. tostring(txt) end
    return txt
  end

  function hand.moves()
    return MOVES_CSV
  end

  function hand.volition(on)
    if on == nil then on = not volition_enabled end
    local ok, err = pcall(function() return arm:volition(on and true or false) end)
    if not ok then
      ctx.log("hand: volition failed: " .. tostring(err))
      return false
    end
    volition_enabled = on and true or false
    ctx.log("hand: volition " .. (volition_enabled and "enabled" or "disabled"))
    return true
  end

  -- Other modules (and future entity-code extensions) get the same API.
  services:register("hand", hand)

  -- ------------------------------------------------ entity calling path
  -- Parse one line of organism output for a hand call. Returns true when
  -- something was dispatched. Function-call form wins over the legacy
  -- colon form.
  -- A quoted name may still carry a trailing duration ("fist 3"); split it
  -- off when no explicit duration argument was given.
  local function split_name_dur(name, dur)
    if dur == nil or dur == "" then
      local inner = string.match(name, "(%d+%.?%d*)%s*$")
      if inner ~= nil then
        dur = inner
        name = string.gsub(name, "%s*%d+%.?%d*%s*$", "")
      end
    end
    return name, dur
  end

  local function parse_call(line)
    local low = string.lower(line)
    -- hand.move("wave", 3) / hand.move('wave') — quoted name, optional dur
    local name, dur = string.match(low, "^%s*hand%.move%s*%(%s*[\"'](.-)[\"']%s*,?%s*(%d*%.?%d*)%s*%)")
    if name == nil then
      -- Lua-call sugar without parens: hand.move "wave"
      name = string.match(low, "^%s*hand%.move%s*[\"'](.-)[\"']")
    end
    if name ~= nil then
      name, dur = split_name_dur(name, dur)
      hand.move(name, tonumber(dur))
      return true
    end
    -- hand.posture("open", 2)
    local pname, pdur = string.match(low, "^%s*hand%.posture%s*%(%s*[\"'](.-)[\"']%s*,?%s*(%d*%.?%d*)%s*%)")
    if pname ~= nil then
      pname, pdur = split_name_dur(pname, pdur)
      hand.posture(pname, tonumber(pdur))
      return true
    end
    -- legacy directive: "hand: wave 3"; trailing words are the organism's
    -- prose; first number on the line, if any, is the duration
    local phrase = string.match(low, "^%s*%[?%s*hand%s*:%s*([^%d]*)")
    if phrase ~= nil then
      phrase = normalize(phrase)
      if phrase ~= "" then
        hand.move(phrase, tonumber(string.match(line, "(%d+%.?%d*)")))
        return true
      end
    end
    return false
  end

  if hooks ~= nil then
    hooks:on("utterance", function(text)
      if text == nil then return end
      -- One move per reply: a bridge goal preempts whatever is running, so a
      -- second call in the same reply would just stomp the first (and extra
      -- calls are usually lines parroted from earlier turns).
      for line in string.gmatch(tostring(text), "[^\n]+") do
        if parse_call(line) then break end
      end
    end)

    -- Lifecycle reactions.
    hooks:on("birth", function(_text)
      if volition_enabled then hand.move("reach", 4.0) end
    end)
    hooks:on("cycle", function(text)
      if not volition_enabled then return end
      if text == "wake" then
        hand.move("wave", 3.0)
      elseif text == "sleep" then
        hand.move("release", 5.0)
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
        return hand.state()
      elseif verb == "posture" then
        local name = args[2] or "open"
        local dur = tonumber(args[3]) or 4.0
        if hand.posture(name, dur) then
          return "hand: posture '" .. name .. "' for " .. dur .. "s"
        end
        return "hand posture failed for '" .. name .. "'"
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
        if hand.move(kind, dur) then
          return "hand: driving goal '" .. kind .. "' for " .. dur .. "s"
        end
        return "hand goal failed for '" .. kind .. "'"
      elseif verb == "volition" then
        local on
        if args[2] then
          on = args[2] == "on" or args[2] == "true"
        else
          on = not volition_enabled  -- bare "/hand volition" toggles
        end
        if hand.volition(on) then
          return "hand: volition " .. (on and "enabled" or "disabled")
        end
        return "hand volition failed"
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

  ctx.log("tendon-hand module loaded; hand.move API armed, volition enabled")
end
