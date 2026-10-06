import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Button } from './Button';

describe('Button', () => {
  it('renders its label and calls onClick when pressed', async () => {
    const onClick = vi.fn();
    const user = userEvent.setup();
    render(<Button onClick={onClick}>Click me</Button>);

    const button = screen.getByRole('button', { name: 'Click me' });
    await user.click(button);

    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('is disabled and shows the reason as a tooltip when disabled', () => {
    render(
      <Button disabled disabledReason="Upload a dataset first">
        Run analysis
      </Button>,
    );

    const button = screen.getByRole('button', { name: 'Run analysis' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('title', 'Upload a dataset first');
  });

  it('marks itself busy and does not fire onClick while loading', async () => {
    const onClick = vi.fn();
    const user = userEvent.setup();
    render(
      <Button loading onClick={onClick}>
        Saving
      </Button>,
    );

    const button = screen.getByRole('button', { name: 'Saving' });
    expect(button).toHaveAttribute('aria-busy', 'true');
    expect(button).toBeDisabled();
    await user.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });
});
