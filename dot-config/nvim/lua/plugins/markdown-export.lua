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

local function export(kind)
	if vim.bo.filetype ~= "markdown" then
		vim.notify("Not a markdown buffer", vim.log.levels.WARN)
		return
	end
	if vim.bo.modified then
		vim.cmd("write")
	end

	local src = vim.api.nvim_buf_get_name(0)
	if src == "" then
		vim.notify("Buffer has no file name", vim.log.levels.WARN)
		return
	end
	if vim.fn.executable("pandoc") == 0 then
		vim.notify("pandoc not found (try: sudo pacman -S pandoc-cli)", vim.log.levels.ERROR)
		return
	end
	if vim.fn.executable("mermaid-filter") == 0 then
		vim.notify("mermaid-filter not found (try: npm install -g mermaid-filter)", vim.log.levels.ERROR)
		return
	end

	local html = vim.fn.fnamemodify(src, ":r") .. ".html"
	local css = vim.fs.joinpath(vim.fn.stdpath("config"), "markdown-export.css")
	local link_filter = vim.fs.joinpath(vim.fn.stdpath("config"), "markdown-export-links.lua")
	-- Root for "/foo.md"-style links: the enclosing git repo, else the file's dir.
	local outdir = vim.fn.fnamemodify(src, ":p:h")
	local link_root = vim.fs.root(src, ".git") or outdir

	local function pandoc_to_html(on_done)
		vim.system(
			{
				"pandoc",
				"--standalone",
				"--embed-resources",
				"--css",
				css,
				"--filter",
				"mermaid-filter",
				"--lua-filter",
				link_filter,
				"--metadata",
				"link-root=" .. link_root,
				"--metadata",
				"link-outdir=" .. outdir,
				"--metadata",
				"pagetitle=" .. vim.fn.fnamemodify(src, ":t:r"),
				"-o",
				html,
				src,
			},
			{
				text = true,
				env = { PUPPETEER_EXECUTABLE_PATH = "/usr/bin/chromium" },
			},
			vim.schedule_wrap(function(res)
				if res.code ~= 0 then
					vim.notify("pandoc failed: " .. res.stderr, vim.log.levels.ERROR)
					return
				end
				on_done()
			end)
		)
	end

	if kind == "html" then
		pandoc_to_html(function()
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
	pandoc_to_html(function()
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

vim.api.nvim_create_user_command("MdToHtml", function()
	export("html")
end, { desc = "Export current markdown buffer to HTML" })

vim.api.nvim_create_user_command("MdToPdf", function()
	export("pdf")
end, { desc = "Export current markdown buffer to PDF" })

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
vim.keymap.set("n", "<leader>mp", "<cmd>MdToPdf<CR>", { desc = "Markdown -> PDF" })
vim.keymap.set("n", "<leader>mt", "<cmd>MdTheme<CR>", { desc = "Toggle PDF theme (auto/light/dark)" })
