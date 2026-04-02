vim.pack.add({
	"https://github.com/nvim-lualine/lualine.nvim",
}, { confirm = false })

local lualine = require("lualine")
lualine.setup({
	options = {
		theme = "tokyonight",
		globalstatus = true,
		disabled_filetypes = {
			statusline = {},
			winbar = {},
		},
	},
})
