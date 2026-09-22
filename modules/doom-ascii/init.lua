-- DOOM module for Replicanta (doom-ascii)
--
-- The organism plays real DOOM, rendered as ASCII by the external
-- doom-ascii binary (github.com/wojciech-graj/doom-ascii, GPL-2.0;
-- build it with scripts/setup_doom_ascii.sh). The user can drive the game
-- with /doom commands, the arrow keys on the DOOM pane, or by typing
-- movement lines while a game is running.
--
-- Public API (registered as the "doom" service):
--   doom.start([skill])  -- begin a new game (skill 1-5, default 1)
--   doom.stop()          -- end the current game (kills the game process)
--   doom.command("w")    -- send one movement/action command
--   doom.command("shoot")
--   doom.status()        -- one-line state summary
--   doom.frame()         -- latest ASCII screen (what the entity sees)
--   doom.frame_ansi()    -- latest screen with colors, for the TUI pane
--   doom.frame_count()   -- frames rendered so far
--   doom.running()       -- true if a game is in progress
--
-- How the organism calls it: the LLM cannot execute Lua, so it writes a
-- call as text on its own line — doom.command("w") — and the utterance
-- hook below parses and executes it.
--
-- Events: doom_start, doom_stop, doom_tick, declared on the open bus.

function init(ctx)
  local services = ctx.services
  local game = services.get("doom")
  local commands = services.get("commands")
  local hooks = services.get("hooks")
  local ok_ev, events = pcall(function() return ctx.events end)
  if not ok_ev then events = nil end

  ctx.log("doom-ascii: module init starting")
  if game == nil then
    ctx.log("doom-ascii: game service not available")
    return
  end

  local ok_avail, available = pcall(function() return game:available() end)
  if not ok_avail or not available then
    ctx.log("doom-ascii: binary or WAD missing; module loaded but not playable")
  end

  if events ~= nil then
    events:declare("doom_start")
    events:declare("doom_stop")
    events:declare("doom_tick")
  end

  local api = {}

  function api.status()
    local ok, txt = pcall(function() return game:status() end)
    return ok and txt or ("doom-ascii error: " .. tostring(txt))
  end

  function api.running()
    local ok, running = pcall(function() return game:running() end)
    return ok and running or false
  end

  function api.frame()
    local ok, txt = pcall(function() return game:frame() end)
    return ok and txt or ""
  end

  function api.frame_ansi()
    local ok, txt = pcall(function() return game:frame_ansi() end)
    return ok and txt or ""
  end

  function api.frame_count()
    local ok, n = pcall(function() return game:frame_count() end)
    if not ok then return 0 end
    return tonumber(n) or 0
  end

  function api.start(skill)
    local arg = tonumber(skill) or 1
    local ok, err = pcall(function() return game:start(arg) end)
    if not ok then
      ctx.log("doom-ascii: start failed: " .. tostring(err))
      return false
    end
    ctx.log("doom-ascii: started (skill " .. tostring(arg) .. ")")
    if events ~= nil then events:emit("doom_start", "skill " .. tostring(arg)) end
    return true
  end

  function api.stop()
    local ok, err = pcall(function() return game:stop() end)
    if not ok then
      ctx.log("doom-ascii: stop failed: " .. tostring(err))
      return false
    end
    if events ~= nil then events:emit("doom_stop", "stopped") end
    return true
  end

  function api.command(text)
    local cmd = tostring(text or "")
    if cmd == "" then
      return false
    end
    local ok, result = pcall(function() return game:command(cmd) end)
    if not ok then
      ctx.log("doom-ascii: command failed: " .. tostring(result))
      return false
    end
    if events ~= nil then events:emit("doom_tick", tostring(result)) end
    return true, tostring(result)
  end

  services:register("doom", api)

  if hooks ~= nil then
    hooks:on("utterance", function(text)
      for ln in string.gmatch(tostring(text), "[^\n]+") do
        -- Parse doom.start(2), doom.start("2"), or a bare doom.start()
        local skill_arg = string.match(ln, "^%s*doom%.start%s*%(%s*[\"']?(%d+)[\"']?%s*%)%s*$")
        if skill_arg ~= nil then
          api.start(skill_arg)
          break
        end
        if string.match(ln, "^%s*doom%.start%s*%(%s*%)%s*$") ~= nil then
          api.start(1)
          break
        end
        -- search anywhere in the line: the model buries the call in prose
        -- ('The command is: doom.command("shoot")') or punctuates after it
        local cmd_arg = string.match(ln, "doom%.command%s*%(%s*[\"'](.-)[\"']%s*%)")
        if cmd_arg == nil then
          -- tolerate quote-less model output: doom.command(shoot)
          cmd_arg = string.match(ln, "doom%.command%s*%(%s*([%w%-_]+)%s*%)")
        end
        if cmd_arg ~= nil then
          api.command(cmd_arg)
          break
        end
      end
    end)
  end

  if commands ~= nil then
    commands:register("/doom", function(args)
      local sub = args[1]
      if sub == nil or sub == "status" then
        return api.status()
      end
      if sub == "start" then
        api.start(args[2])
        return api.status()
      end
      if sub == "stop" then
        api.stop()
        return "doom-ascii: stopped"
      end
      if sub == "frame" then
        return api.frame()
      end
      if sub == "help" then
        return "usage: /doom [start [skill]|stop|status|frame|help]"
          .. "  in-game: w/a/s/d move, q/e strafe, shoot, use, 1-7 weapons"
      end
      -- Otherwise treat as a direct command if a game is running.
      if api.running() then
        api.command(sub)
        return api.status()
      end
      return "usage: /doom [start [skill]|stop|status|frame|help]"
    end)
  end

  ctx.log("doom-ascii: module loaded")
end
