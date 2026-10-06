import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Card, Skeleton } from "./Surfaces";

describe("Card", () => {
  it("renders its content", () => {
    render(<Card>Dataset summary</Card>);
    expect(screen.getByText("Dataset summary")).toBeInTheDocument();
  });

  it("is keyboard-activatable when interactive", async () => {
    const onClick = vi.fn();
    const user = userEvent.setup();
    render(
      <Card interactive onClick={onClick}>
        Open dataset
      </Card>,
    );

    const card = screen.getByRole("button", { name: "Open dataset" });
    card.focus();
    await user.keyboard("{Enter}");

    expect(onClick).toHaveBeenCalled();
  });
});

describe("Skeleton", () => {
  it("is decorative (hidden from assistive tech)", () => {
    const { container } = render(<Skeleton variant="card" />);
    expect(container.firstChild).toHaveAttribute("aria-hidden", "true");
  });

  it("renders the requested number of table rows", () => {
    const { container } = render(<Skeleton variant="table" rows={5} />);
    expect(container.firstElementChild?.children).toHaveLength(5);
  });
});
