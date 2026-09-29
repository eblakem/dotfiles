-- Pandoc Lua filter used by lua/plugins/markdown-export.lua.
-- Rewrites local links so they work when the exported HTML is opened via file://
--   * "/foo/bar.md" (repo-root relative) -> relative to the output file's directory
--   * "x.md" -> exported HTML when it's part of this export set (tree export),
--     else "x.html" when that exported HTML exists next to the target
-- Expects metadata: link-root (repo root), link-outdir (output file's directory).
-- Optional metadata:
--   link-srcdir  source file's directory (defaults to link-outdir)
--   link-set     file listing absolute .md paths exported in this run
--   link-mirror  output root those files are mirrored into (relative to link-root)
--   link-list    "true": don't rewrite, print resolved local .md targets to stdout
--                (used to crawl links for the tree export)

local path = pandoc.path
local root, outdir, srcdir, mirror
local set = {}
local list_mode = false

local function exists(p)
	local f = io.open(p, "r")
	if f then
		f:close()
		return true
	end
	return false
end

-- Collapse "." and ".." segments (pandoc.path.normalize leaves "..").
local function collapse(p)
	local parts = {}
	for seg in p:gmatch("[^/]+") do
		if seg == ".." then
			table.remove(parts)
		elseif seg ~= "." then
			table.insert(parts, seg)
		end
	end
	return "/" .. table.concat(parts, "/")
end

local function meta_str(meta, key)
	return meta[key] and pandoc.utils.stringify(meta[key])
end

local function Meta(meta)
	root = meta_str(meta, "link-root")
	outdir = meta_str(meta, "link-outdir")
	srcdir = meta_str(meta, "link-srcdir") or outdir
	mirror = meta_str(meta, "link-mirror")
	list_mode = meta_str(meta, "link-list") == "true"
	local set_file = meta_str(meta, "link-set")
	if set_file then
		for line in io.lines(set_file) do
			set[line] = true
		end
	end
end

-- Returns (absolute path, fragment) for a local link target, or nil.
local function resolve(target)
	-- Leave external links, anchors, and protocol-relative URLs alone.
	if not srcdir or target:match("^%a[%w+.-]*:") or target:match("^#") or target:match("^//") then
		return nil
	end

	local file, frag = target:match("^([^#]*)(#?.*)$")
	if file == "" then
		return nil
	end

	if file:sub(1, 1) == "/" then
		if not root then
			return nil
		end
		return collapse(path.join({ root, file:sub(2) })), frag
	end
	return collapse(path.join({ srcdir, file })), frag
end

local function Link(el)
	local abs, frag = resolve(el.target)
	if not abs then
		return nil
	end

	if list_mode then
		if abs:match("%.md$") then
			io.stdout:write(abs, "\n")
		end
		return nil
	end

	if abs:match("%.md$") then
		if set[abs] and mirror and root then
			abs = path.join({ mirror, path.make_relative(abs, root) }):gsub("%.md$", ".html")
		else
			local html = abs:gsub("%.md$", ".html")
			if exists(html) then
				abs = html
			end
		end
	end

	el.target = path.make_relative(abs, outdir, true) .. frag
	return el
end

return { { Meta = Meta }, { Link = Link } }
