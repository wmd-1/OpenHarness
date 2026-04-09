import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import React from 'react';
import {render} from 'ink';

import {ThemeProvider} from '../theme/ThemeContext.js';
import {MarkdownText} from './MarkdownText.js';

const stripAnsi = (value: string): string => value.replace(/\u001B\[[0-9;?]*[ -/]*[@-~]/g, '');

type InkTestStdout = PassThrough & {
	isTTY: boolean;
	columns: number;
	rows: number;
	cursorTo: () => boolean;
	clearLine: () => boolean;
	moveCursor: () => boolean;
};

function createTestStdout(): InkTestStdout {
	return Object.assign(new PassThrough(), {
		isTTY: true,
		columns: 120,
		rows: 40,
		cursorTo: () => true,
		clearLine: () => true,
		moveCursor: () => true,
	});
}

async function renderTableLines(content: string): Promise<string[]> {
	const stdout = createTestStdout();

	let output = '';
	stdout.on('data', (chunk) => {
		output += chunk.toString();
	});

	const instance = render(
		<ThemeProvider initialTheme="default">
			<MarkdownText content={content} />
		</ThemeProvider>,
		{stdout: stdout as unknown as NodeJS.WriteStream, debug: true, patchConsole: false},
	);

	await new Promise((resolve) => setTimeout(resolve, 80));
	instance.unmount();
	instance.cleanup();
	await new Promise((resolve) => setTimeout(resolve, 20));

	return stripAnsi(output)
		.split('\n')
		.filter((line) => /[┌├│└]/.test(line))
		.slice(0, 5);
}

test('keeps table borders aligned when cells contain inline markdown', async () => {
	const lines = await renderTableLines('| `aa` | bb |\n|------|----|\n| c | **ddd** |');

	assert.equal(lines.length, 5);

	const widths = lines.map((line) => [...line].length);
	assert.ok(
		widths.every((width) => width === widths[0]),
		`Expected table lines to share a width, got ${JSON.stringify(
			lines.map((line, index) => ({line, width: widths[index]})),
		)}`,
	);
});
