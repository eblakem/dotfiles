-- Export the current markdown buffer to HTML/PDF via pandoc (+ chromium for PDF).
-- No plugin needed: just pandoc + chromium --headless under the hood.

-- Headless chromium doesn't read GTK/dconf, so it won't pick up the system's
-- dark-mode preference on its own; ask gsettings and force it to match.
local function preferred_color_scheme()
	if vim.fn.executable("gsettings") == 0 then
		return 1 -- light
	end
	local out = vim.fn.system({ "gsettings", "get", "org.gnome.desktop.interface", "color-scheme" })
	return out:find("dark") and 2 or 1
end

-- "auto" follows the system (via preferred_color_scheme); :MdTheme overrides it.
local pdf_theme = "auto"

local function resolved_pdf_theme()
	if pdf_theme == "auto" then
		return preferred_color_scheme() == 2 and "dark" or "light"
	end
	return pdf_theme
end

local css = vim.fs.joinpath(vim.fn.stdpath("config"), "markdown-export.css")
local link_filter = vim.fs.joinpath(vim.fn.stdpath("config"), "markdown-export-links.lua")
-- :MdToHtmlTree writes into a mirror of the repo under here, so the generated
-- files stay out of the repo and :MdExportClean can wipe them in one go.
local tree_root = vim.fs.joinpath(vim.fn.stdpath("cache"), "md-export")

-- Root for "/foo.md"-style links: the enclosing git repo, else the file's dir.
local function link_root_for(src)
	return vim.fs.root(src, ".git") or vim.fn.fnamemodify(src, ":p:h")
end

-- Returns the source file of the current markdown buffer (saved), or nil.
local function current_markdown_file()
	if vim.bo.filetype ~= "markdown" then
		vim.notify("Not a markdown buffer", vim.log.levels.WARN)
		return nil
	end
	if vim.bo.modified then
		vim.cmd("write")
	end

	local src = vim.api.nvim_buf_get_name(0)
	if src == "" then
		vim.notify("Buffer has no file name", vim.log.levels.WARN)
		return nil
	end
	if vim.fn.executable("pandoc") == 0 then
		vim.notify("pandoc not found (try: sudo pacman -S pandoc-cli)", vim.log.levels.ERROR)
		return nil
	end
	if vim.fn.executable("mermaid-filter") == 0 then
		vim.notify("mermaid-filter not found (try: npm install -g mermaid-filter)", vim.log.levels.ERROR)
		return nil
	end
	return src
end

-- Converts src -> html. extra_meta adds pandoc metadata (e.g. the tree
-- export's link-set/link-mirror). on_done(err) gets nil on success.
local function pandoc_to_html(src, html, extra_meta, on_done)
	local srcdir = vim.fn.fnamemodify(src, ":p:h")
	local args = {
		"pandoc",
		"--standalone",
		"--embed-resources",
		"--resource-path",
		srcdir .. ":.",
		"--css",
		css,
		"--filter",
		"mermaid-filter",
		"--lua-filter",
		link_filter,
		"--metadata",
		"link-root=" .. link_root_for(src),
		"--metadata",
		"link-srcdir=" .. srcdir,
		"--metadata",
		"link-outdir=" .. vim.fn.fnamemodify(html, ":p:h"),
		"--metadata",
		"pagetitle=" .. vim.fn.fnamemodify(src, ":t:r"),
	}
	for key, value in pairs(extra_meta or {}) do
		vim.list_extend(args, { "--metadata", key .. "=" .. value })
	end
	vim.list_extend(args, { "-o", html, src })

	vim.system(
		args,
		{
			text = true,
			env = { PUPPETEER_EXECUTABLE_PATH = "/usr/bin/chromium" },
		},
		vim.schedule_wrap(function(res)
			on_done(res.code ~= 0 and res.stderr or nil)
		end)
	)
end

local function export(kind)
	local src = current_markdown_file()
	if not src then
		return
	end
	local html = vim.fn.fnamemodify(src, ":r") .. ".html"

	if kind == "html" then
		pandoc_to_html(src, html, nil, function(err)
			if err then
				vim.notify("pandoc failed: " .. err, vim.log.levels.ERROR)
				return
			end
			vim.notify("Exported " .. vim.fn.fnamemodify(html, ":t"))
			vim.ui.open(html)
		end)
		return
	end

	-- kind == "pdf": pandoc -> html -> chromium headless print-to-pdf
	if vim.fn.executable("chromium") == 0 then
		vim.notify("chromium not found, needed for PDF export", vim.log.levels.ERROR)
		return
	end
	pandoc_to_html(src, html, nil, function(err)
		if err then
			vim.notify("pandoc failed: " .. err, vim.log.levels.ERROR)
			return
		end
		-- Headless chromium's print pipeline doesn't reliably honor
		-- prefers-color-scheme emulation, so bake the theme in directly.
		if resolved_pdf_theme() == "dark" then
			local lines = vim.fn.readfile(html)
			for i, line in ipairs(lines) do
				local patched, n = line:gsub("<html", '<html data-theme="dark"', 1)
				if n > 0 then
					lines[i] = patched
					break
				end
			end
			vim.fn.writefile(lines, html)
		end

		local pdf = vim.fn.fnamemodify(src, ":r") .. ".pdf"
		vim.system({
			"chromium",
			"--headless",
			"--disable-gpu",
			"--no-pdf-header-footer",
			"--print-to-pdf=" .. pdf,
			"file://" .. html,
		}, { text = true }, vim.schedule_wrap(function(res)
			vim.fn.delete(html)
			if res.code ~= 0 then
				vim.notify("chromium pdf export failed: " .. res.stderr, vim.log.levels.ERROR)
				return
			end
			vim.notify("Exported " .. vim.fn.fnamemodify(pdf, ":t"))
			vim.ui.open(pdf)
		end))
	end)
end

-- Local .md files linked from src (absolute paths), found by running pandoc
-- with the link filter in list mode — so reference links etc. are handled.
local function linked_markdown(src, root)
	local res = vim.system({
		"pandoc",
		"--lua-filter",
		link_filter,
		"--metadata",
		"link-root=" .. root,
		"--metadata",
		"link-srcdir=" .. vim.fn.fnamemodify(src, ":p:h"),
		"--metadata",
		"link-list=true",
		"-o",
		"/dev/null",
		src,
	}, { text = true }):wait()
	if res.code ~= 0 then
		return {}
	end
	return vim.split(res.stdout, "\n", { trimempty = true })
end

-- Breadth-first crawl from src, up to max_depth link hops. Only follows files
-- that exist inside root (the mirror can't represent anything outside it).
local function crawl(src, root, max_depth)
	local seen = { [src] = true }
	local files = { src }
	local frontier = { src }
	for _ = 1, max_depth do
		local next_frontier = {}
		for _, file in ipairs(frontier) do
			for _, target in ipairs(linked_markdown(file, root)) do
				if not seen[target] and vim.startswith(target, root .. "/") and vim.uv.fs_stat(target) then
					seen[target] = true
					table.insert(files, target)
					table.insert(next_frontier, target)
				end
			end
		end
		frontier = next_frontier
	end
	return files
end

-- Exports src plus everything it links to (up to depth hops) into a mirror
-- of the repo under tree_root, with links between them pointing at the HTML.
local function export_tree(depth)
	local src = current_markdown_file()
	if not src then
		return
	end
	src = vim.fs.normalize(vim.fn.fnamemodify(src, ":p"))
	local root = link_root_for(src)
	local mirror = vim.fs.joinpath(tree_root, vim.fn.fnamemodify(root, ":t"))

	local files = crawl(src, root, depth)
	local set_file = vim.fs.joinpath(mirror, ".export-set")
	vim.fn.mkdir(mirror, "p")
	vim.fn.writefile(files, set_file)

	local function html_for(file)
		return (vim.fs.joinpath(mirror, vim.fs.relpath(root, file)):gsub("%.md$", ".html"))
	end

	vim.notify(("Exporting %d files to %s ..."):format(#files, vim.fn.fnamemodify(mirror, ":~")))

	-- mermaid-filter spins up headless chromium, so cap the parallelism.
	local max_jobs = 4
	local next_index, running, failures = 1, 0, {}
	local function pump()
		if next_index > #files and running == 0 then
			if #failures > 0 then
				vim.notify(
					("Exported %d/%d files; failed:\n%s"):format(#files - #failures, #files, table.concat(failures, "\n")),
					vim.log.levels.WARN
				)
			else
				vim.notify(("Exported %d files"):format(#files))
			end
			vim.ui.open(html_for(src))
			return
		end
		while running < max_jobs and next_index <= #files do
			local file = files[next_index]
			next_index = next_index + 1
			running = running + 1
			local html = html_for(file)
			vim.fn.mkdir(vim.fn.fnamemodify(html, ":h"), "p")
			pandoc_to_html(file, html, { ["link-set"] = set_file, ["link-mirror"] = mirror }, function(err)
				running = running - 1
				if err then
					table.insert(failures, vim.fs.relpath(root, file) .. ": " .. vim.trim(err))
				end
				pump()
			end)
		end
	end
	pump()
end

vim.api.nvim_create_user_command("MdToHtml", function()
	export("html")
end, { desc = "Export current markdown buffer to HTML" })

vim.api.nvim_create_user_command("MdToPdf", function()
	export("pdf")
end, { desc = "Export current markdown buffer to PDF" })

vim.api.nvim_create_user_command("MdToHtmlTree", function(opts)
	local depth = tonumber(opts.args) or 2
	export_tree(depth)
end, {
	nargs = "?",
	desc = "Export current markdown buffer + linked files (default depth 2) to HTML",
})

vim.api.nvim_create_user_command("MdExportClean", function()
	if vim.fn.isdirectory(tree_root) == 0 then
		vim.notify("Nothing to clean")
		return
	end
	vim.fn.delete(tree_root, "rf")
	vim.notify("Removed " .. vim.fn.fnamemodify(tree_root, ":~"))
end, { desc = "Delete all :MdToHtmlTree output" })

vim.api.nvim_create_user_command("MdTheme", function(opts)
	local arg = opts.args ~= "" and opts.args or nil
	if arg then
		if arg ~= "auto" and arg ~= "light" and arg ~= "dark" then
			vim.notify("MdTheme: expected auto|light|dark", vim.log.levels.ERROR)
			return
		end
		pdf_theme = arg
	else
		local next_theme = { auto = "light", light = "dark", dark = "auto" }
		pdf_theme = next_theme[pdf_theme]
	end
	local suffix = pdf_theme == "auto" and (" (currently " .. resolved_pdf_theme() .. ")") or ""
	vim.notify("PDF export theme: " .. pdf_theme .. suffix)
end, {
	nargs = "?",
	complete = function()
		return { "auto", "light", "dark" }
	end,
	desc = "Set/cycle the theme used by :MdToPdf (auto follows the system)",
})

vim.keymap.set("n", "<leader>mh", "<cmd>MdToHtml<CR>", { desc = "Markdown -> HTML" })
vim.keymap.set("n", "<leader>mH", "<cmd>MdToHtmlTree<CR>", { desc = "Markdown + linked files -> HTML" })
vim.keymap.set("n", "<leader>mc", "<cmd>MdExportClean<CR>", { desc = "Clean linked-files HTML export" })
vim.keymap.set("n", "<leader>mp", "<cmd>MdToPdf<CR>", { desc = "Markdown -> PDF" })
vim.keymap.set("n", "<leader>mt", "<cmd>MdTheme<CR>", { desc = "Toggle PDF theme (auto/light/dark)" })
