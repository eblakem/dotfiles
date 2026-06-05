vim.pack.add({
	"https://github.com/folke/sidekick.nvim",
}, { confirm = false })

require("sidekick").setup({
	cli = {
		tools = {
			agy = {
				cmd = { "agy" },
			},
		},
	},
})

local vk = vim.keymap
vk.set("n", "<leader>aa", function()
	require("sidekick.cli").toggle({ name = "agy", focus = true })
end, { silent = true, desc = "Agy toggle" })
