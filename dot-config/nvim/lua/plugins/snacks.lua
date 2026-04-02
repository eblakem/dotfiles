vim.pack.add({
	"https://github.com/folke/snacks.nvim",
}, { confirm = false })

require("snacks").setup({
	expolorer = { enabled = true },
	picker = {},
	notifier = {
		top_down = false,
		margin = { bottom = 2 },
		style = "minimal",
	},
	terminal = {
		win = { height = 0.25 },
		shell = shell,
	},
	dashboard = {
		enabled = true,
		preset = {
			pick = "telescope.nvim",
			keys = {
				{
					icon = " ",
					key = "f",
					desc = "Find File",
					action = function()
						require("telescope.builtin").find_files({ hidden = false })
					end,
				},
				{ icon = " ", key = "g", desc = "Find Text", action = ":Telescope live_grep" },
				{ icon = " ", key = "r", desc = "Recent Files", action = ":Telescope oldfiles" },
				{
					icon = " ",
					mode = "n",
					key = "\\",
					desc = "File Browser",
					action = ":lua Snacks.explorer()",
				},
				{ icon = " ", key = "s", desc = "Restore Session", section = "session" },
				{
					icon = " ",
					icon_hl = "Title",
					desc = "Terminal",
					key = "t",
					action = ":lua Snacks.terminal()",
				},
				{
					icon = " ",
					key = "c",
					desc = "Config",
					action = function()
						require("telescope.builtin").find_files({ cwd = vim.fn.stdpath("config") })
					end,
				},
				{ icon = " ", key = "q", desc = "Quit", action = ":qa" },
			},
		},
		sections = {
			{ section = "header" },
			{
				pane = 2,
				{ section = "keys", gap = 1, padding = 1 },
			},
		},
	},
	styles = {
		notifier = {
			backdrop = false,
		},
	},
})

local vk = vim.keymap
vk.set("n", "\\", ":lua Snacks.explorer()<cr>", { silent = true, desc = "File Browser" })
vk.set("n", "<leader>fc", function()
	require("telescope.builtin").find_files({ cwd = vim.fn.stdpath("config") })
end, { silent = true, desc = "Nvim Config" })
