vim.pack.add({
	"https://github.com/nvim-mini/mini.indentscope",
}, { confirm = false })

local indentscope = require("mini.indentscope")
indentscope.setup({
	symbol = "│",
	options = { try_as_border = true },
})
vim.api.nvim_create_autocmd("FileType", {
	group = augroup,
	pattern = {
		"snacks_dashboard",
		"fzf",
		"help",
		"lazy",
		"mason",
	},
	callback = function()
		vim.b.miniindentscope_disable = true
	end,
})
vim.api.nvim_create_autocmd("User", {
	group = augroup,
	pattern = "SnacksDashboardOpened",
	callback = function(data)
		vim.b[data.buf].miniindentscope_disable = true
		vim.o.laststatus = 3
	end,
})
