vim.pack.add({
	"https://github.com/folke/tokyonight.nvim",
}, { confirm = false })

require("tokyonight").setup({
	style = "night",
	styles = {
		sidebars = "transparent",
		floats = "transparent",
	},
	transparent = true,
	terminal_colors = true,
	---@param c ColorScheme
	on_colors = function(c)
		c.bg_statusline = nil
	end,
})
vim.cmd.colorscheme("tokyonight")
