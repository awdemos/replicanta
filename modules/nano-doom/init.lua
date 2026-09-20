-- Nano DOOM module for Replicanta
--
-- The organism can play a tiny first-person shooter that renders in the
-- chat window as ASCII. The user can drive it with /doom commands or by
-- typing movement lines while a game is running.
--
-- Public API (registered as the "doom" service):
--   doom.start()                    -- start a new game on the default map
--   doom.start(map_name)            -- start a named map
--   doom.stop()                     -- end the current game
--   doom.command("w")                -- send one movement/shoot command
--   doom.command("shoot")
--   doom.status()                  -- one-line state summary
--   doom.running()                   -- true if a game is in progress
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

  ctx.log("nano-doom: module init starting")
  if game == nil then
    ctx.log("nano-doom: game service not available")
    return
  end

  local ok_avail, available = pcall(function() return game:available() end)
  if not ok_avail or not available then
    ctx.log("nano-doom: bridge unavailable; module loaded but not playable")
  end

  if events ~= nil then
    events:declare("doom_start")
    events:declare("doom_stop")
    events:declare("doom_tick")
  end

  local api = {}

  function api.tactical()
    local ok, txt = pcall(function() return game:tactical() end)
    return ok and txt or ""
  end

  function api.can_shoot()
    local ok, result = pcall(function() return game:command("__can_shoot") end)
    if not ok then return false end
    return result == "yes"
  end

  function api.status()
    local ok, txt = pcall(function() return game:status() end)
    return ok and txt or ("nano-doom error: " .. tostring(txt))
  end

  function api.running()
    local ok, running = pcall(function() return game:running() end)
    return ok and running or false
  end

  function api.start(map_name)
    local ok, err = pcall(function() return game:start(map_name or "default") end)
    if not ok then
      ctx.log("nano-doom: start failed: " .. tostring(err))
      return false
    end
    ctx.log("nano-doom: started " .. tostring(map_name or "default"))
    if events ~= nil then events:emit("doom_start", tostring(map_name or "default")) end
    return true
  end

  function api.stop()
    local ok, err = pcall(function() return game:stop() end)
    if not ok then
      ctx.log("nano-doom: stop failed: " .. tostring(err))
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
      ctx.log("nano-doom: command failed: " .. tostring(result))
      return false
    end
    if events ~= nil then events:emit("doom_tick", tostring(result)) end
    return true, tostring(result)
  end

  services:register("doom", api)

  if hooks ~= nil then
    hooks:on("utterance", function(text)
      for ln in string.gmatch(tostring(text), "[^\n]+") do
        -- Parse doom.start("map") or doom.command("x")
        local map_arg = string.match(ln, "^%s*doom%.start%s*%(%s*[\"'](.-)[\"']?%s*%)%s*$")
        if map_arg ~= nil then
          api.start(map_arg)
          break
        end
        local cmd_arg = string.match(ln, "^%s*doom%.command%s*%(%s*[\"'](.-)[\"']%s*%)%s*$")
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
        return "nano-doom: stopped"
      end
      if sub == "maps" then
        local ok, result = pcall(function()
          return game:command("__maps")
        end)
        if not ok then
          return "maps error: " .. tostring(result)
        end
        return "maps: " .. tostring(result)
      end
      if sub == "help" then
        return "usage: /doom [start [map]|stop|status|maps|help]  in-game: w/a/s/d to move, q/e turn, shoot"
      end
      -- Otherwise treat as a direct command if a game is running.
      if api.running() then
        api.command(sub)
        return api.status()
      end
      return "usage: /doom [start [map]|stop|status|maps|help]"
    end)
  end

  ctx.log("nano-doom: module loaded")
end
