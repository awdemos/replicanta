-- DOOM module for Replicanta (doom-ascii) — pure Lua over the capability
-- bridges. No Python service: the module spawns the external game through
-- ctx.process (pty recipe), parses frames from the raw stream, and exposes
-- the same "doom" service API the TUI, web, and narration already consume.
--
-- The organism plays real DOOM, rendered as ASCII by the external
-- doom-ascii binary (github.com/wojciech-graj/doom-ascii, GPL-2.0;
-- build it with scripts/setup_doom_ascii.sh). The user can drive the game
-- with /doom commands, the arrow keys on the DOOM overlay, or by typing
-- movement lines while a game is running.
--
-- Public API (registered as the "doom" service):
--   doom.start([skill])  -- begin a new game (skill 1-5, default 1)
--   doom.stop()          -- end the current game (kills the game process)
--   doom.command("w")    -- send one movement/action command
--   doom.command("shoot")
--   doom.status()        -- one-line state summary
--   doom.frame()         -- latest ASCII screen (what the entity sees)
--   doom.frame_ansi()    -- latest screen with colors, for the pane
--   doom.frame_count()   -- frames rendered so far
--   doom.running()       -- true if a game is in progress
--
-- How the organism plays: the LLM cannot execute Lua, so it writes moves as
-- text on its own line — doom.command("w") — and the utterance hook below
-- executes them for a RUNNING game. Starting a game is never parseable from
-- model text (process creation from free prose is the blast-radius hole):
-- games begin via /doom, the arrow keys, or the controller's explicit
-- auto-restart path. Entity-issued play is additionally gated by the
-- organism's entity_actuation flag and yields to manual play.
--
-- Events: doom_start, doom_stop, doom_tick, declared on the open bus.

function init(ctx)
  local services = ctx.services
  local externals = services.get("externals")
  local commands = services.get("commands")
  local hooks = services.get("hooks")
  local ok_ev, events = pcall(function() return ctx.events end)
  if not ok_ev then events = nil end
  local ok_call, svc_call = pcall(function() return ctx.call end)
  if not ok_call then svc_call = nil end

  ctx.log("doom-ascii: module init starting")

  if events ~= nil then
    events:declare("doom_start")
    events:declare("doom_stop")
    events:declare("doom_tick")
  end

  -- Frame wire protocol (doomgeneric_ascii.c): every frame is one write
  -- starting with the cursor-home escape and ending with an SGR reset.
  local CURSOR_HOME = "\27[;H"
  local SGR_RESET = "\27[0m"
  -- Plain-frame budget for the entity's text view: a scaling-2 frame is
  -- 160 cols x 50 rows (~8050 chars); the cap must never cut rows off the
  -- bottom where health/ammo live.
  local FRAME_PROMPT_CHARS = 12000
  local FRAME_ANSI_CHARS = 240000
  local START_TIMEOUT = 8.0

  -- Budget caps slice raw bytes; block characters are multi-byte UTF-8, so
  -- a blind sub can split a sequence and poison every downstream decode.
  local function utf8_safe_cut(s, n)
    s = s:sub(1, n)
    while #s > 0 and s:byte(-1) >= 0x80 do
      s = s:sub(1, -2) -- walk back past continuation/lead bytes to ASCII
    end
    return s
  end

  local KEYMAP = {
    w = "\27[A",
    s = "\27[B",
    d = "\27[C",
    a = "\27[D",
    q = ",",
    e = ".",
    shoot = " ",
    fire = " ",
    use = "e",
    open = "e",
    ["1"] = "1",
    ["2"] = "2",
    ["3"] = "3",
    ["4"] = "4",
    ["5"] = "5",
    ["6"] = "6",
    ["7"] = "7",
  }

  -- Spaced taps per command. The engine registers a key for only ~42ms per
  -- read, so one tap moves the player a barely-visible step (forward/back
  -- felt broken to humans while turns, rotating the whole view, read fine).
  -- Taps arrive ~50ms apart, keeping the key refreshed for a visible step;
  -- the flush window between reads eats some, which extra taps absorb.
  local TAPS = {
    w = 8,
    s = 8,
    a = 3,
    d = 3,
    q = 6,
    e = 6,
  }

  local game = {
    proc = nil,
    token = 0,
    starting = false,
    deadline = 0.0,
    start_error = nil,
    over = false,
    buffer = "",
    ansi_frame = "",
    plain_frame = "",
    frame_count = 0,
    skill = 1,
    viewport_cols = 80, -- app terminal width; drives the scaling choice
    yield_until = 0.0, -- entity play yields to the human until this clock
  }

  local function strip_ansi(s)
    s = s:gsub("\27%][^\7\27]*(\7|\27\\)", "") -- OSC titles
    s = s:gsub("\27%[[0-9;?]*[A-Za-z]", "") -- SGR / cursor codes
    return s
  end

  -- Split the decoded stream into (complete frames, pending tail). A chunk
  -- is a frame once the NEXT cursor-home arrives and it carries the SGR
  -- reset the engine emits at the end of every frame.
  local function split_frames(buf)
    local frames = {}
    local pending = buf
    while true do
      local home = pending:find(CURSOR_HOME, 1, true)
      if home == nil then
        break
      end
      local nxt = pending:find(CURSOR_HOME, home + 1, true)
      local chunk
      if nxt then
        chunk = pending:sub(home, nxt - 1)
      else
        chunk = pending:sub(home)
      end
      local body = chunk:sub(#CURSOR_HOME + 1)
      if body:sub(-#SGR_RESET) == SGR_RESET then
        frames[#frames + 1] = body
        pending = nxt and pending:sub(nxt) or ""
        if nxt == nil then
          break
        end
      else
        -- In-progress frame: keep it (home included) for the next chunk.
        pending = pending:sub(home)
        break
      end
    end
    return frames, pending
  end

  -- Reader-thread callback: invoked under the host lua_lock.
  local function pump(chunk)
    game.buffer = game.buffer .. chunk
    local frames, pending = split_frames(game.buffer)
    game.buffer = pending
    for _, frame in ipairs(frames) do
      local ansi = frame:gsub("\27%][^\7\27]*(\7|\27\\)", "")
      game.ansi_frame = ansi
      local plain = strip_ansi(ansi):gsub("^\n+", ""):gsub("\n+$", "")
      game.plain_frame = plain
      game.frame_count = game.frame_count + 1
      game.starting = false
      game.start_error = nil
      game.over = false
    end
  end

  local function do_stop()
    game.token = game.token + 1
    if game.proc ~= nil then
      pcall(function()
        ctx.process.kill(game.proc)
      end)
      game.proc = nil
    end
    game.starting = false
  end

  -- ------------------------------------------------------------ public API
  local api = {}

  function api.status()
    if game.start_error ~= nil then
      return "doom-ascii: " .. game.start_error .. " — /doom start retries"
    end
    if game.proc == nil then
      if game.over then
        return string.format(
          "doom-ascii: game over after %d frames — /doom start to play again",
          game.frame_count
        )
      end
      return "no game running — /doom start begins DOOM"
    end
    if game.frame_count == 0 then
      return string.format("doom-ascii: starting (skill %d)…", game.skill)
    end
    return string.format(
      "doom-ascii: skill %d · frame %d · running — /doom stop ends it",
      game.skill,
      game.frame_count
    )
  end

  function api.running()
    return game.proc ~= nil
  end

  function api.set_viewport(cols)
    game.viewport_cols = tonumber(cols) or 80
  end

  function api.yield_to_human(secs)
    -- Human took the keyboard: entity-issued play (entity_command, and
    -- utterance-hook doom lines) stays quiet until this clock.
    game.yield_until = ctx.clock() + (tonumber(secs) or 30)
  end

  function api.entity_command(text)
    if ctx.clock() < game.yield_until then
      return false
    end
    return api.command(text)
  end

  function api.frame()
    return utf8_safe_cut(game.plain_frame, FRAME_PROMPT_CHARS)
  end

  function api.frame_ansi()
    return utf8_safe_cut(game.ansi_frame, FRAME_ANSI_CHARS)
  end

  function api.frame_count()
    return game.frame_count
  end

  function api.start(skill, timeout)
    if game.starting then
      return true -- a start is already in flight; do not double-spawn
    end
    local arg = tonumber(skill) or 1
    if arg < 1 then
      arg = 1
    elseif arg > 5 then
      arg = 5
    end
    game.skill = arg
    if externals == nil then
      ctx.log("doom-ascii: externals service missing; cannot start")
      return false
    end
    local ok_bin, bin = pcall(function()
      return externals:doom_binary()
    end)
    local ok_wad, wad = pcall(function()
      return externals:doom_wad()
    end)
    if not ok_bin or not ok_wad or bin == nil or wad == nil then
      ctx.log("doom-ascii: binary or WAD missing; run scripts/setup_doom_ascii.sh")
      return false
    end
    do_stop()
    local my_token = game.token
    game.over = false
    game.start_error = nil
    game.buffer = ""
    game.ansi_frame = ""
    game.plain_frame = ""
    game.frame_count = 0
    -- Block characters are the only charset a human can actually play:
    -- gradient letters are unreadable soup at game speed. -nograd paints
    -- full blocks, -fixgamma offsets their darkening. -warp 1 1 sets
    -- autostart in d_main.c, skipping the title screen and demo playback
    -- (without it keys only skip demos — never control the marine).
    -- Scaling: the TUI paints the frame as half-block cells (two vertical
    -- pixels per cell), so scaling 4 (80x50 px) fills the classic 80x25
    -- footprint and scaling 2 (160x100 px) fills a wide terminal. Extras
    -- go FIRST so a duplicated flag in DOOM_ASCII_ARGS wins (M_CheckParm
    -- takes the first match).
    local scaling = game.viewport_cols >= 166 and "2" or "4"
    local argv = { bin }
    local extra = externals:doom_args()
    if extra ~= nil then
      for tok in string.gmatch(tostring(extra), "%S+") do
        argv[#argv + 1] = tok
      end
    end
    table.insert(argv, "-iwad")
    table.insert(argv, wad)
    table.insert(argv, "-scaling")
    table.insert(argv, scaling)
    table.insert(argv, "-skill")
    table.insert(argv, tostring(arg))
    table.insert(argv, "-chars")
    table.insert(argv, "block")
    table.insert(argv, "-nograd")
    table.insert(argv, "-fixgamma")
    table.insert(argv, "-warp")
    table.insert(argv, "1")
    table.insert(argv, "1")
    local pid
    local ok_spawn, spawn_err = pcall(function()
      pid = ctx.process.spawn(argv, {
        pty = true,
        on_data = pump,
        on_exit = function(_code)
          -- Generation-guarded: only the current game may flip state; a
          -- kill from api.stop bumps the token first, making this a no-op.
          if game.token == my_token then
            game.proc = nil
            game.over = game.frame_count > 0
          end
          -- Reap the finished child so it stops counting against the
          -- 2-process cap.
          pcall(function()
            ctx.process.kill(pid)
          end)
        end,
      })
    end)
    if not ok_spawn then
      ctx.log("doom-ascii: start failed: " .. tostring(spawn_err))
      return false
    end
    game.proc = pid
    game.starting = true
    game.deadline = ctx.clock() + (tonumber(timeout) or START_TIMEOUT)
    ctx.log("doom-ascii: started (skill " .. tostring(arg) .. ")")
    if events ~= nil then
      events:emit("doom_start", "skill " .. tostring(arg))
    end
    return true
  end

  function api.stop()
    local had = game.proc ~= nil
    do_stop()
    game.start_error = nil
    game.over = false
    if had and events ~= nil then
      events:emit("doom_stop", "stopped")
    end
    return true
  end

  function api.command(text)
    local cmd = tostring(text or "")
    if cmd == "" then
      return false
    end
    if cmd == "start" then
      api.start()
      return true, api.frame()
    end
    if game.proc == nil then
      error("no game running")
    end
    local seq = KEYMAP[cmd]
    if seq == nil then
      error("unknown doom command '" .. cmd .. "'")
    end
    ctx.process.write(game.proc, seq, { taps = TAPS[cmd] or 1, spacing = 0.05 })
    if events ~= nil then
      events:emit("doom_tick", api.frame())
    end
    return true, api.frame()
  end

  services:register("doom", api)

  -- Watchdog: a game that never paints (bad WAD, hung binary) is reaped on
  -- the cycle tick and its own stderr becomes the visible error.
  if hooks ~= nil then
    hooks:on("cycle", function()
      if game.starting and game.proc ~= nil and ctx.clock() > game.deadline then
        local err = "produced no frame in time"
        local ok_tail, tail = pcall(function()
          return ctx.process.stderr(game.proc)
        end)
        if ok_tail and tail ~= nil then
          tail = tail:gsub("%s+", " ")
          if #tail > 0 then
            err = err .. ": " .. tail:sub(-200)
          end
        end
        do_stop()
        game.start_error = err
        ctx.log("doom-ascii: start failed: " .. err)
      end
    end)
  end

  if externals == nil or externals:doom_binary() == nil or externals:doom_wad() == nil then
    ctx.log("doom-ascii: binary or WAD missing; module loaded but not playable")
  end

  -- ---------------------------------------------------- entity calling path
  -- Entity actuation gate: the utterance hook is entity-initiated, so
  -- process/physical actions it executes consult the organism's
  -- entity_actuation flag. User-initiated paths (/doom, arrow keys, web
  -- buttons) never pass here and are not gated. Hosts without the organism
  -- facade (or test doubles) default to enabled, the historical behavior.
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

  if hooks ~= nil then
    hooks:on("utterance", function(text)
      -- Every organism utterance passes here. Only doom.command lines for
      -- a RUNNING game execute — doom.start was deliberately removed from
      -- this path: process creation must not be reachable from free model
      -- text. Moves respect the human yield (the utterance hook is
      -- entity-initiated play, unlike /doom and the arrow keys).
      local yielded = ctx.clock() < game.yield_until
      local actuated = actuation_enabled()
      for ln in string.gmatch(tostring(text), "[^\n]+") do
        -- search anywhere in the line: the model buries the call in prose
        -- ('The command is: doom.command("shoot")') or punctuates after it
        local snippet = string.match(ln, "doom%.command%s*%([^)]*%)")
        if snippet ~= nil then
          local cmd_arg = string.match(snippet, "^%s*doom%.command%s*%(%s*[\"'](.-)[\"']%s*%)")
          if cmd_arg == nil then
            -- tolerate quote-less model output: doom.command(shoot)
            cmd_arg = string.match(snippet, "^%s*doom%.command%s*%(%s*([%w%-_]+)%s*%)")
          end
          cmd_arg = string.lower(cmd_arg or "")
          -- "start" would spawn a process from prose: it is not a move
          if cmd_arg ~= "" and cmd_arg ~= "start" then
            if not actuated then
              ctx.log("actuation disabled — skipping doom.command")
            elseif yielded then
              -- the human holds the keyboard; the move would no-op anyway
            elseif svc_call ~= nil then
              -- one shared tolerant parser/router (ctx.call): executes the
              -- whitelisted move against the running game
              pcall(function()
                svc_call(snippet)
              end)
            else
              pcall(function()
                api.entity_command(cmd_arg)
              end)
            end
            break
          end
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
        pcall(function()
          api.command(sub)
        end)
        return api.status()
      end
      return "usage: /doom [start [skill]|stop|status|frame|help]"
    end)
  end

  ctx.log("doom-ascii: module loaded")
end
