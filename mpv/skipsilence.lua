-- skipsilence.lua — speed through the pauses in speech.
--
-- Loaded into the podcast player's mpv with --script. While enabled it keeps a
-- silencedetect filter in the audio chain and watches mpv's log for its
-- "silence_start" / "silence_end" lines. During a pause the playback speed
-- ramps up from the base speed towards `speed_max`; when the voice comes back
-- the base speed is restored. Nothing is cut, so time-pos stays honest and
-- seeking behaves as usual. The technique is the one NewPipe's "fast-forward
-- during silence" and ferreum's mpv-skipsilence use; this is an independent,
-- deliberately small implementation.
--
-- Script messages (send with `script-message-to skipsilence <name> ...`):
--   enable | disable | toggle        turn the feature on or off
--   set-speed <number>               set the base speed (what the user picked)
-- Properties for the daemon:
--   user-data/skipsilence/enabled    bool
--   user-data/skipsilence/base_speed number
--
-- Options (--script-opts=skipsilence-<name>=<value>):
--   enabled            start enabled (default no)
--   threshold_db       silence threshold in dB (default -30)
--   threshold_duration seconds of quiet before it counts (default 0.15)
--   speed_max          ceiling while in silence (default 2.5)
--   ramp_start         multiplier applied the moment silence starts (default 1.2)
--   ramp_rate          extra multiplier per second of silence (default 0.6)
--   update_interval    seconds between speed updates in silence (default 0.1)

local mp = require "mp"
local msg = require "mp.msg"
local options = require "mp.options"

local opts = {
  enabled = false,
  threshold_db = -30,
  threshold_duration = 0.15,
  speed_max = 2.5,
  ramp_start = 1.2,
  ramp_rate = 0.6,
  update_interval = 0.1,
}
options.read_options(opts, "skipsilence")

local FILTER_LABEL = "skipsilence_silencedetect"
local FILTER_ID = "skipsilence"

local state = {
  enabled = false,
  base_speed = 1.0,
  in_silence = false,
  silence_started = nil,   -- mp.get_time() when silence began
  timer = nil,
  applying = false,        -- true while this script itself sets `speed`
  filter_added = false,
}

local function clamp(value, low, high)
  if value < low then return low end
  if value > high then return high end
  return value
end

local function publish()
  mp.set_property_native("user-data/skipsilence/enabled", state.enabled)
  mp.set_property_native("user-data/skipsilence/base_speed", state.base_speed)
end

local function set_speed(value)
  state.applying = true
  mp.set_property_number("speed", value)
  state.applying = false
end

local function filter_spec()
  return "@" .. FILTER_LABEL .. ":lavfi=[silencedetect@" .. FILTER_ID
    .. "=n=" .. tostring(opts.threshold_db) .. "dB:d=" .. tostring(opts.threshold_duration) .. "]"
end

local function add_filter()
  if state.filter_added then return end
  mp.commandv("af", "add", filter_spec())
  state.filter_added = true
end

local function remove_filter()
  if not state.filter_added then return end
  mp.commandv("af", "remove", "@" .. FILTER_LABEL)
  state.filter_added = false
end

local function stop_timer()
  if state.timer then
    state.timer:kill()
    state.timer = nil
  end
end

local function leave_silence(restore)
  stop_timer()
  if state.in_silence then
    state.in_silence = false
    state.silence_started = nil
    if restore then set_speed(state.base_speed) end
  end
end

local function tick()
  if not state.in_silence or not state.enabled then return end
  if mp.get_property_native("core-idle") then return end   -- paused or buffering
  local elapsed = mp.get_time() - (state.silence_started or mp.get_time())
  local target = state.base_speed * (opts.ramp_start + opts.ramp_rate * elapsed)
  target = clamp(target, state.base_speed, math.max(state.base_speed, opts.speed_max))
  local current = mp.get_property_number("speed") or state.base_speed
  if math.abs(current - target) > 0.005 then set_speed(target) end
end

local function enter_silence()
  if not state.enabled or state.in_silence then return end
  state.in_silence = true
  state.silence_started = mp.get_time()
  stop_timer()
  state.timer = mp.add_periodic_timer(opts.update_interval, tick)
  tick()
end

-- "[ffmpeg] silencedetect@skipsilence: silence_start: 6.07669"
-- "[ffmpeg] silencedetect@skipsilence: silence_end: 7.06 | silence_duration: 0.98"
local function on_log(event)
  if not state.enabled or event.prefix ~= "ffmpeg" then return end
  local text = event.text or ""
  if text:find("silencedetect", 1, true) ~= 1 then return end
  if text:find("silence_start", 1, true) then
    enter_silence()
  elseif text:find("silence_end", 1, true) then
    leave_silence(true)
  end
end

local function on_speed(_, value)
  if state.applying or value == nil then return end
  -- Somebody else (the daemon, a key binding) changed the speed: that is the
  -- new base speed, whether or not we are mid-ramp.
  if not state.in_silence then
    state.base_speed = value
    publish()
  end
end

local function on_seek_or_start()
  leave_silence(true)
end

local function enable()
  if state.enabled then return end
  state.enabled = true
  state.base_speed = mp.get_property_number("speed") or 1.0
  add_filter()
  mp.register_event("log-message", on_log)
  mp.observe_property("speed", "number", on_speed)
  mp.register_event("seek", on_seek_or_start)
  mp.register_event("start-file", on_seek_or_start)
  mp.register_event("end-file", on_seek_or_start)
  publish()
  msg.info("enabled (base speed " .. tostring(state.base_speed) .. ")")
end

local function disable()
  if not state.enabled then
    publish()
    return
  end
  leave_silence(true)
  state.enabled = false
  mp.unregister_event(on_log)
  mp.unobserve_property(on_speed)
  mp.unregister_event(on_seek_or_start)
  remove_filter()
  publish()
  msg.info("disabled")
end

local function toggle()
  if state.enabled then disable() else enable() end
end

local function set_base_speed(value)
  local speed = tonumber(value)
  if not speed or speed <= 0 then return end
  state.base_speed = speed
  publish()
  if state.in_silence then
    tick()
  else
    set_speed(speed)
  end
end

-- The filter chain is per file: re-add ours whenever a new file starts.
mp.register_event("file-loaded", function()
  if state.enabled then
    state.filter_added = false
    for _, f in ipairs(mp.get_property_native("af") or {}) do
      if f.label == FILTER_LABEL then state.filter_added = true end
    end
    add_filter()
  end
end)

mp.enable_messages("v")
mp.register_script_message("enable", enable)
mp.register_script_message("disable", disable)
mp.register_script_message("toggle", toggle)
mp.register_script_message("set-speed", set_base_speed)

publish()
if opts.enabled then enable() end
