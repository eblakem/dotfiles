-- Pandoc Lua filter used by lua/plugins/markdown-export.lua.
-- Rewrites local links so they work when the exported HTML is opened via file://
--   * "/foo/bar.md" (repo-root relative) -> relative to the output file's directory
--   * "x.md" -> "x.html" when that exported HTML exists next to the target
-- Expects metadata: link-root (repo root), link-outdir (output file's directory).

local path = pandoc.path
local root, outdir

local function exists(p)
	local f = io.open(p, "r")
	if f then
		f:close()
		return true
	end
	return false
end

local function Meta(meta)
	root = meta["link-root"] and pandoc.utils.stringify(meta["link-root"])
	outdir = meta["link-outdir"] and pandoc.utils.stringify(meta["link-outdir"])
end

local function Link(el)
	local target = el.target
	-- Leave external links, anchors, and protocol-relative URLs alone.
	if not outdir or target:match("^%a[%w+.-]*:") or target:match("^#") or target:match("^//") then
		return nil
	end

	local file, frag = target:match("^([^#]*)(#?.*)$")
	if file == "" then
		return nil
	end

	local abs
	if file:sub(1, 1) == "/" then
		if not root then
			return nil
		end
		abs = path.join({ root, file:sub(2) })
	else
		abs = path.join({ outdir, file })
	end

	if abs:match("%.md$") then
		local html = abs:gsub("%.md$", ".html")
		if exists(html) then
			abs = html
		end
	end

	el.target = path.make_relative(abs, outdir, true) .. frag
	return el
end

return { { Meta = Meta }, { Link = Link } }
