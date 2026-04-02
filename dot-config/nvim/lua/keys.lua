local vk = vim.keymap
vk.set({ "n" }, "<Esc><Esc>", "<Esc>:nohlsearch<CR>", { silent = true })
vk.set({ "n" }, "<leader>sk", ":Telescope keymaps<CR>", { desc = "Keymaps" })
vk.set("n", "<Leader>gg", function()
	require("neogit").open({ cwd = vim.fn.expand("%:p:h") })
end)

vk.set({ "n" }, "<leader>tt", ":lua Snacks.terminal()<CR>", { desc = "Terminal" })
