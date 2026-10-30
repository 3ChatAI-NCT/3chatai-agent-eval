const { heroui } = require("@heroui/react");

module.exports = {
  content: ["./index.html", "./src/**/*.{ts,tsx}",
    "./node_modules/@heroui/**/theme/dist/**/*.{js,mjs}",
    "./node_modules/@heroui/theme/dist/**/*.{js,mjs}"],
  theme: { extend: {} },
  darkMode: "class",
  plugins: [heroui()],
};
