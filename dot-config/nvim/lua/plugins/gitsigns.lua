vim.pack.add({
	"https://github.com/lewis6991/gitsigns.nvim",
}, { confirm = false })

require("gitsigns").setup({
	current_line_blame = true,
	current_line_blame_opts = {
		delay = 300,
	},
})
