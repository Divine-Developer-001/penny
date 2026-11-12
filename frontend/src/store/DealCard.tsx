/** Renders one Channel3 offer (or a labelled sample offer): merchant, price, delta vs the store price, link. */
import type { DealOffer } from "../types";
import { money, signedMoney } from "../format";

interface Props {
  offer: DealOffer;
  sample: boolean;
}

export default function DealCard({ offer, sample }: Props) {
  const cheaper = Number(offer.delta) < 0;
  return (
    <div className="deal">
      <div className="deal-main">
        <b>{offer.merchant}</b>
        <span className="deal-title">{offer.title}</span>
      </div>
      <div className="deal-price">
        <b>{money(offer.price)}</b>
        <span className={cheaper ? "good" : "muted"}>{Number(offer.delta) === 0 ? "same as store" : `${signedMoney(offer.delta)} vs store`}</span>
      </div>
      {offer.link && !sample ? (
        <a className="mini" href={offer.link} target="_blank" rel="noopener noreferrer">View</a>
      ) : (
        <span className="tag-sample">sample data</span>
      )}
    </div>
  );
}
