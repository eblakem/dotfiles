vim.pack.add({
	"https://github.com/stevearc/conform.nvim",
}, { confirm = false })

local conform = require("conform")
conform.setup({
	default_format_opts = {
		lsp_format = "fallback",
	},
	formatters_by_ft = formatters,
})

local function cleanup_trailing_spaces()
	if vim.bo.filetype == "markdown" then
		return
	end
	vim.cmd([[%s/\s\+$//e]])
end

vim.api.nvim_create_autocmd("BufWritePre", {
	group = augroup,
	pattern = "*",
	callback = function(args)
		cleanup_trailing_spaces()
		conform.format({ bufnr = args.buf })
	end,
})
