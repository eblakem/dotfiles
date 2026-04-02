vim.pack.add({
	"https://github.com/neovim/nvim-lspconfig",
	"https://github.com/mason-org/mason-lspconfig.nvim",
	"https://github.com/WhoIsSethDaniel/mason-tool-installer.nvim",
}, { confirm = false })

local augroup = vim.api.nvim_create_augroup("UserUIAuto", { clear = true })
vim.api.nvim_create_autocmd("LspProgress", {
	group = augroup,
	---@param ev {data: {client_id: integer, params: lsp.ProgressParams}}
	callback = function(ev)
		local spinner = { "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏" }
		vim.notify(vim.lsp.status(), "info", {
			id = "lsp_progress",
			title = "LSP Progress",
			opts = function(notif)
				notif.icon = ev.data.params.value.kind == "end" and " "
					or spinner[math.floor(vim.uv.hrtime() / (1e6 * 80)) % #spinner + 1]
			end,
		})
	end,
})

require("mason-lspconfig").setup({ ensure_installed = ensure_installed })

local augroup = vim.api.nvim_create_augroup("UserCodingAuto", { clear = true })

vim.api.nvim_create_autocmd("LspAttach", {
	group = augroup,
	desc = "LSP actions",
	callback = function(ev)
		local vk = vim.keymap
		local b = vim.lsp.buf

		vk.set("n", "K", b.hover, { buffer = ev.buf, desc = "Hover" })
		vk.set("n", "gd", b.definition, { buffer = ev.buf, desc = "Go to definition" })
		vk.set("n", "gD", b.declaration, { buffer = ev.buf, desc = "Go to declaration" })
		vk.set("n", "gi", b.implementation, { buffer = ev.buf, desc = "Go to implementation" })
		vk.set("n", "go", b.type_definition, { buffer = ev.buf, desc = "Go to type definition" })
		vk.set("n", "gr", b.references, { buffer = ev.buf, desc = "References", nowait = true })
		vk.set("n", "gs", b.signature_help, { buffer = ev.buf, desc = "Signatures" })
		vk.set({ "n", "i" }, "<F2>", b.rename, { buffer = ev.buf, desc = "Rename" })
		vk.set({ "n", "i" }, "<F4>", b.code_action, { buffer = ev.buf, desc = "Code action" })
	end,
})

vim.api.nvim_create_autocmd("BufEnter", {
	once = true,
	group = augroup,
	pattern = "*.lua",
	callback = function()
		require("lazydev").setup({
			library = {
				{ path = "${3rd}/luv/library", words = { "vim%.uv" } },
				{ path = "wezterm-types", mods = { "wezterm" } },
			},
		})
	end,
})

local formatters = {
	sh = { "shfmt" },
	lua = { "stylua" },
	rust = { "rustfmt" },
}
local prettierft = {
	"css",
	"less",
	"scss",
	"javascript",
	"javascriptreact",
	"typescript",
	"typescriptreact",
	"html",
	"json",
	"yaml",
	"markdown",
}
for _, ft in ipairs(prettierft) do
	formatters[ft] = { "prettierd" }
end

local tools = {}
for _, fmts in pairs(formatters) do
	for _, fmt in ipairs(fmts) do
		if fmt == "rustfmt" then
			-- rustfmt should not be installed from mason
			goto skip
		end
		if not vim.tbl_contains(tools, fmt) then
			table.insert(tools, fmt)
		end
		::skip::
	end
end

require("mason-tool-installer").setup({ ensure_installed = tools })
