-- exposure_control.lua
-- Live-Regler fuer Astras Auto-Exposure, ohne andere Conf-Opts anzufassen
-- (get -> modify -> set statt komplettem Ersetzen der Options-Liste).
--
--   Ctrl+Shift+4/5  auto_exposure_limit_postive runter/hoch (Schritt 0.1)
--   Ctrl+Shift+6    Limit-Override entfernen (Conf/Default gilt wieder)
--   Ctrl+Shift+7/8  auto_exposure_anchor runter/hoch (Schritt 0.005)
--   Ctrl+Shift+9    Anchor-Override entfernen (Conf/Default gilt wieder)
--
-- Hinweis Mechanik: Das Limit kappt, wie viele Blendenstufen die
-- Automatik Richtung Anchor aufhellen darf. Shader-Default ist 0.0
-- (kein Aufhellen). Fuer dunkle Master: Limit hochdrehen; die
-- Automatik nutzt nur so viel davon, wie das Material braucht.

local ANCHOR_DEFAULT = 0.6   -- Astra-Default
local LIMIT_DEFAULT  = 0.0   -- Astra-Default
local ANCHOR_STEP    = 0.005
local LIMIT_STEP     = 0.1

local function get_opts()
    return mp.get_property_native("glsl-shader-opts") or {}
end

local function current(key, fallback)
    local v = tonumber(get_opts()[key])
    return v or fallback
end

local function set_opt(key, value, fmt)
    local opts = get_opts()
    opts[key] = value and string.format(fmt, value) or nil
    mp.set_property_native("glsl-shader-opts", opts)
end

local function osd(key, value)
    if value then
        mp.osd_message(string.format("%s: %s", key, value), 2)
    else
        mp.osd_message(string.format("%s: reset (Conf/Default)", key), 2)
    end
end

-- ---- Limit (der Alltagsregler fuer dunkle Master) ----

local function limit_change(delta)
    local v = current("auto_exposure_limit_postive", LIMIT_DEFAULT) + delta
    v = math.max(0.0, math.min(5.0, v))
    set_opt("auto_exposure_limit_postive", v, "%.1f")
    osd("limit_postive", string.format("%.1f EV", v))
end

mp.add_key_binding("ctrl+shift+4", "limit_down", function() limit_change(-LIMIT_STEP) end, {repeatable = true})
mp.add_key_binding("ctrl+shift+5", "limit_up",   function() limit_change( LIMIT_STEP) end, {repeatable = true})
mp.add_key_binding("ctrl+shift+6", "limit_reset", function()
    set_opt("auto_exposure_limit_postive", nil)
    osd("limit_postive", nil)
end)

-- ---- Anchor (Feinjustage des Zielwerts) ----

local function anchor_change(delta)
    local v = current("auto_exposure_anchor", ANCHOR_DEFAULT) + delta
    v = math.max(0.1, math.min(1.0, v))
    set_opt("auto_exposure_anchor", v, "%.3f")
    osd("anchor", string.format("%.3f", v))
end

mp.add_key_binding("ctrl+shift+7", "anchor_down", function() anchor_change(-ANCHOR_STEP) end, {repeatable = true})
mp.add_key_binding("ctrl+shift+8", "anchor_up",   function() anchor_change( ANCHOR_STEP) end, {repeatable = true})
mp.add_key_binding("ctrl+shift+9", "anchor_reset", function()
    set_opt("auto_exposure_anchor", nil)
    osd("anchor", nil)
end)
